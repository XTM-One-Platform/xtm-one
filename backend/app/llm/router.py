# SPDX-License-Identifier: Apache-2.0

"""LLM router – resolves the correct provider **and model** for a given context.

LLM provider configs are **platform-wide** (not per-user).  The resolution
priority for chat/reasoning is:

1. Explicit ``model_override`` (e.g. from agent config or GA config)
2. The agent default model (from ``PlatformSettings.agent_default_model``)
3. User preference (from ``User.preferred_llm_model``)
4. Platform default (from ``GeneralAssistantConfig.llm_model``)
5. Any configured platform provider - the first chat-capable config in the
   order Settings lists them (oldest first, tie-broken by id), else the first
   non-embedding-only one on its built-in chat model (``implicit_chat_config``)

Embedding and image generation resolution reads the admin-configured model
from ``PlatformSettings``, falling back to sensible defaults
(OpenAI ``text-embedding-3-large`` / ``gpt-image-1``, Gemini fallback).

When the resolved primary model fails (rate limit, overload, transient
provider error) after retries are exhausted, ``get_llm_for_user`` wraps the
provider in a :class:`FailoverLLMProvider`.  An admin-configured explicit
fallback model (``PlatformSettings.fallback_model`` / ``DEFAULT_FALLBACK_MODEL``)
is tried first on its owning provider, before the generic per-provider chain.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, ClassVar

import openai
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_value
from app.core.utils import format_exception_message
from app.llm import video_served
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.attribution import (
    config_identity,
    record_failover_served,
    record_primary_served,
)
from app.llm.azure_openai_provider import AzureOpenAIProvider
from app.llm.base import (
    BaseLLMProvider,
    ContextOverflowError,
    ModelDeclinedError,
    StreamUsageSink,
    gateway_error_envelope,
    provider_max_tools,
    usage_sink_of,
)
from app.llm.bedrock_provider import BedrockProvider
from app.llm.custom_openai_provider import CustomOpenAIProvider
from app.llm.elevenlabs_provider import ElevenLabsProvider
from app.llm.gateway_provider import FiligranGatewayProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.identity import set_llm_caller
from app.llm.kling_provider import KlingProvider
from app.llm.litellm_provider import LiteLLMProvider
from app.llm.lmstudio_provider import LMStudioProvider
from app.llm.mistral_provider import MistralProvider
from app.llm.model_capabilities import decline_fallback_models
from app.llm.model_limits import resolve_provider_max_output_tokens
from app.llm.ollama_provider import OllamaProvider
from app.llm.openai_compat import gateway_upstream_refusal
from app.llm.openai_provider import OpenAIProvider
from app.llm.openrouter_provider import OpenRouterProvider
from app.llm.provider_access import (
    ProviderNotUsableError,
    ProviderRouting,
    bind_acting_user,
    ensure_not_excluded,
    is_restricted,
    provider_routing,
    usable_model_ids,
)
from app.llm.provider_errors import error_body, error_status
from app.llm.runway_provider import RunwayProvider
from app.llm.seedance_provider import SeedanceProvider
from app.llm.tei_embedding_provider import TEIEmbeddingProvider
from app.llm.vertex_ai_provider import VertexAIProvider
from app.llm.xai_provider import XAIProvider
from app.models.llm_config import LLMProviderConfig
from app.services.failure_causes import exception_status_code
from app.services.vault import get_resolver, reveal_field, stored_reference_providers
from app.tools.base import tool_retention_key

logger = logging.getLogger(__name__)

# ── Provider instance cache ──────────────────────────────────────────────────
# Avoids repeated Fernet decrypt + SDK client creation for the same provider
# config.  Entries expire after _PROVIDER_CACHE_TTL seconds.  Thread-safe via
# a simple lock (the event loop is single-threaded but workers may import).

_PROVIDER_CACHE_TTL = 120  # seconds

_provider_cache: dict[str, tuple[BaseLLMProvider, float]] = {}
_provider_cache_lock = threading.Lock()
# The digest of the key a cached instance was built with, for keys read from a
# vault: the instance is reused only while the vault hands over that key.
_provider_key_digests: dict[str, bytes] = {}

# Provider configs we've already warned about exposing no chat-capable model.
# ``build_provider`` runs on every provider rebuild (every _PROVIDER_CACHE_TTL
# seconds), so without this guard an OpenAI provider used only for Image
# Generation / Embeddings (``available_models`` = image/embedding models) would
# re-log the same WARNING every ~2 minutes forever.
# Keyed by ``provider:id`` and cleared by ``invalidate_provider_cache`` so an
# admin fixing the config re-arms the warning. Guarded by its own lock (not
# ``_provider_cache_lock``): ``build_provider`` mutates it OUTSIDE the cache lock
# and is a public entry point, so concurrent builds / a racing
# ``invalidate_provider_cache`` would otherwise make the de-dup non-deterministic.
_warned_no_chat_model: set[str] = set()
_warned_lock = threading.Lock()

# Configured fallback-model values we've already warned about in
# ``get_llm_for_user`` (non-chat-capable, or not owned by any configured
# provider). ``get_llm_for_user`` is a hot path (every chat turn and
# assignment run), so a single misconfigured ``fallback_model`` would
# otherwise flood the logs with an identical WARNING until fixed.
# Warn once per process per configured VALUE: a changed value re-warns
# naturally (new key), and ``invalidate_provider_cache`` re-arms the set so
# an admin who fixes (or newly breaks) the config gets a fresh evaluation.
# Guarded by ``_warned_lock`` alongside ``_warned_no_chat_model``.
_warned_fallback_model: set[str] = set()

# Failover targets whose endpoint refused a chat call because the model cannot
# chat: ``(provider config id, model) -> monotonic expiry``.
# ``is_chat_capable_model`` fails open for ids missing from the catalog, so the
# endpoint's own refusal is what keeps failover from calling that model again.
# Keyed by the config, not the instance (``_get_cached_provider`` rebuilds one
# every ``_PROVIDER_CACHE_TTL``) nor the provider type (sibling configs of one
# type can point at different endpoints). Per process on purpose: a missing
# entry costs one refused call before the chain moves on, a shared store would
# cost a round trip on every failover. Nothing tells another process that the
# settings changed, hence the expiry; ``invalidate_provider_cache`` clears it
# in this one. Guarded by ``_warned_lock``.
_REFUSED_CHAT_TARGET_TTL = 15 * 60  # seconds
_MAX_REFUSED_CHAT_TARGETS = 256
_refused_chat_targets: dict[tuple[str, str], float] = {}

# Lowercased substrings of an endpoint refusing a chat call for the MODEL itself
# (OpenAI, ``param: "model"``): a completion-only model answers 404 "This is not
# a chat model and thus not supported in the v1/chat/completions endpoint", a
# responses-only one 404 "This model is only supported in v1/responses and not
# in v1/chat/completions" (LiteLLM relays that one as a 400, ``param`` kept).
_NON_CHAT_MODEL_ERROR_MARKERS: tuple[str, ...] = (
    "not a chat model",
    "this model is only supported in v1/responses",
)


def is_non_chat_model_error(exc: BaseException) -> bool:
    """Whether *exc* is an endpoint refusing a chat call because its model cannot chat.

    Narrow on purpose, since a match takes the model out of failover: the
    message must name the model as what cannot chat, and the error must be a
    404 or point at the ``model`` parameter. A rate limit, an overload, an auth
    error, a timeout, a context-length error, an unknown model or a 400 about
    another parameter never matches.
    """
    msg = str(exc).lower()
    if not any(marker in msg for marker in _NON_CHAT_MODEL_ERROR_MARKERS):
        return False
    return getattr(exc, "status_code", None) == 404 or getattr(exc, "param", None) == "model"


def _chat_refusal_key(provider: BaseLLMProvider, model: str) -> tuple[str, str] | None:
    """``(config id, model)`` for a target built from a config, else ``None``."""
    config_id = getattr(provider, "provider_config_id", None)
    return (config_id, model) if config_id else None


def _is_refused_chat_target(provider: BaseLLMProvider, model: str) -> bool:
    """Whether *provider*'s endpoint refused *model* as non-chat less than a TTL ago."""
    key = _chat_refusal_key(provider, model)
    if key is None:
        return False
    with _warned_lock:
        expires = _refused_chat_targets.get(key)
        if expires is None:
            return False
        if expires > time.monotonic():
            return True
        del _refused_chat_targets[key]
        return False


def _remember_refused_chat_target(provider: BaseLLMProvider, model: str) -> bool:
    """Leave *model* on *provider*'s config out of failover; ``True`` when newly so.

    A provider built without a config (a test double, a hand-wired chain) is not
    remembered: only a config id outlives the instance.
    """
    key = _chat_refusal_key(provider, model)
    if key is None:
        return False
    now = time.monotonic()
    with _warned_lock:
        was_refused = _refused_chat_targets.pop(key, 0.0) > now
        while len(_refused_chat_targets) >= _MAX_REFUSED_CHAT_TARGETS:
            del _refused_chat_targets[next(iter(_refused_chat_targets))]
        _refused_chat_targets[key] = now + _REFUSED_CHAT_TARGET_TTL
    return not was_refused


# Provider/model-specific kwargs that are unsafe to forward across provider
# boundaries during failover (can trigger 400 invalid-argument errors):
#   - ``structured_output`` — formatted for the primary provider's native
#     schema-enforcement surface (OpenAI/Gemini ``response_format``,
#     Anthropic tool-use schemas) and won't translate to the fallback.
#   - ``reasoning_effort`` — only meaningful for the o-series / reasoning-
#     capable OpenAI models; fallback providers raise on the unknown key.
#
# ``temperature`` is intentionally NOT in this set: it's a universal
# sampling parameter that every provider accepts, and stripping it would
# regress the caller's intent (e.g. an agent run requesting deterministic
# temperature=0 would silently get the fallback's default 0.7). Note that
# in ``chat()`` and ``chat_stream_with_tools()`` ``temperature`` does
# arrive inside the ``**kwargs`` passed to ``_failover_call`` (each
# wrapper forwards its explicit ``temperature=temperature`` argument);
# the ``chat_stream()`` failover path is the only call site where
# ``temperature`` is a local variable threaded explicitly into the
# fallback call rather than through the dict filtered here.
_FALLBACK_EXCLUDED_KWARGS = frozenset({"structured_output", "reasoning_effort"})


def _cached_entry(config: LLMProviderConfig, max_age: float = _PROVIDER_CACHE_TTL) -> BaseLLMProvider | None:
    now = time.monotonic()
    with _provider_cache_lock:
        entry = _provider_cache.get(f"{config.provider}:{config.id}")
        if entry and (now - entry[1]) < max_age:
            return entry[0]
    return None


def _cache_provider(
    config: LLMProviderConfig, provider: BaseLLMProvider, key_digest: bytes | None = None
) -> BaseLLMProvider:
    cache_key = f"{config.provider}:{config.id}"
    with _provider_cache_lock:
        _provider_cache[cache_key] = (provider, time.monotonic())
        if key_digest is None:
            _provider_key_digests.pop(cache_key, None)
        else:
            _provider_key_digests[cache_key] = key_digest
    return provider


async def _cached_provider(config: LLMProviderConfig) -> BaseLLMProvider:
    """Return a cached provider instance, building one if missing/expired.

    The API key is read through the vault resolver: a key stored as a
    vault reference is resolved on every call (from the resolver's memory
    within ``SECRET_VAULT_CACHE_SECONDS``) and the cached instance is reused
    only while it was built with that key, so a rotation is picked up as soon
    as the resolver hands the new value over, never later.
    """
    if not stored_reference_providers(config, ("api_key_enc",)):
        return _get_cached_provider(config)
    api_key = await reveal_field(config, "api_key_enc") or ""
    key_digest = hashlib.sha256(api_key.encode()).digest()
    provider = _cached_entry(config)
    with _provider_cache_lock:
        built_with = _provider_key_digests.get(f"{config.provider}:{config.id}")
    if provider is None or built_with != key_digest:
        provider = _cache_provider(config, build_provider(config, api_key=api_key), key_digest)
    return provider


async def provider_for_config(config: LLMProviderConfig) -> BaseLLMProvider:
    """The provider instance serving *config*, from the shared cache.

    For a caller that already chose the config itself (and checked it may use
    it, ``app.llm.provider_access``) and calls an endpoint chat routing knows
    nothing about, such as a decision model.
    """
    return await _cached_provider(config)


def _get_cached_provider(config: LLMProviderConfig) -> BaseLLMProvider:
    """:func:`_cached_provider` for the synchronous failover chain.

    It cannot read a vault: a referenced key is served from the instance the
    async path cached, and only while the resolver would still serve that key
    from memory (``SECRET_VAULT_CACHE_SECONDS``, never past the instance TTL);
    otherwise it is refused (``decrypt_value`` raises on a reference), which
    skips that fallback rather than sending a stale key or the reference. A
    failover chain gets the instances :func:`warm_referenced_providers`
    prepared for its call instead.
    """
    return _sync_provider(config, referenced=bool(stored_reference_providers(config, ("api_key_enc",))))


def _sync_provider(config: LLMProviderConfig, *, referenced: bool) -> BaseLLMProvider:
    max_age = min(_PROVIDER_CACHE_TTL, get_resolver().cache_seconds) if referenced else _PROVIDER_CACHE_TTL
    provider = _cached_entry(config, max_age)
    if provider is not None:
        return provider
    return _cache_provider(config, build_provider(config))


async def warm_referenced_providers(configs: Iterable[LLMProviderConfig]) -> dict[str, BaseLLMProvider]:
    """The providers whose key is a vault reference, built or re-checked ahead of a failover.

    Keyed by config id: the chain built for this call uses them as they are,
    whatever the cache times, so a key read now is the key it fails over with.
    """
    prepared: dict[str, BaseLLMProvider] = {}
    for config in configs:
        if stored_reference_providers(config, ("api_key_enc",)):
            try:
                prepared[str(config.id)] = await _cached_provider(config)
            except Exception as exc:
                logger.debug("Fallback provider %s not prepared: %s", config.provider, format_exception_message(exc))
    return prepared


def invalidate_provider_cache() -> None:
    """Clear the provider cache (call after admin changes LLM settings)."""
    with _provider_cache_lock:
        _provider_cache.clear()
        _provider_key_digests.clear()
    # Re-arm the "no chat-capable model" warning so a config the admin just
    # fixed (or newly broke) is re-evaluated on next build instead of being
    # permanently suppressed by a stale dedupe entry. Cleared under its own lock
    # (kept separate from ``_provider_cache_lock`` so no path nests the two).
    # The fallback-model warning dedupe and the learned chat refusals are
    # re-armed for the same reason.
    with _warned_lock:
        _warned_no_chat_model.clear()
        _warned_fallback_model.clear()
        _refused_chat_targets.clear()


def _should_warn_fallback_model(model_id: str) -> bool:
    """One-shot dedupe for the hot-path fallback-model warnings.

    Returns ``True`` exactly once per process per *model_id* (until
    :func:`invalidate_provider_cache` re-arms the set). Check-and-add runs
    under ``_warned_lock``; the caller logs OUTSIDE the lock so exactly one
    concurrent caller emits the warning without holding the lock across the
    logging call (same discipline as the ``_warned_no_chat_model`` dedupe in
    :func:`build_provider`).
    """
    with _warned_lock:
        if model_id in _warned_fallback_model:
            return False
        _warned_fallback_model.add(model_id)
        return True


# Lowercased substrings identifying a request-size / payload error — an HTTP
# 413 ``request_too_large`` from Anthropic, Gemini's 400 "Request payload size
# exceeds the limit: NNN bytes", a generic gateway "payload too large" /
# "request entity too large", or a provider content-size limit. A 413 whose
# ``error.code`` is ``request_too_large`` counts whatever its wording.
# Read only through :func:`is_request_too_large_error`, shared by
# :meth:`FailoverLLMProvider._is_non_failoverable` (an oversized payload
# reproduces on every provider, so failing over just burns the whole chain)
# and :func:`is_request_too_large_error` (the graceful-handling
# predicate used by ``agent_chat`` / ``general_chat`` / ``public_chat`` /
# ``assignment_engine`` and the chat error mapper
# ``api/v1/chat._friendly_error_message``) so failover classification and
# user-facing messages can never drift. A provider's own body cap
# (``body_cap_error_code``) still fails over to other providers.
REQUEST_TOO_LARGE_KEYWORDS: tuple[str, ...] = (
    "request_too_large",
    "request exceeds the maximum size",
    "content size limit",
    "payload too large",
    "request entity too large",
    "payload size exceeds the limit",
    # Anthropic's request-wide PDF cap ("A maximum of 600 PDF pages may be
    # provided"): the attachments of the conversation, not one file, are what
    # is too large - the same shape as a 413 for the user, the classifier and
    # the failover chain alike.
    "pdf pages may be provided",
)

# User-facing message for HTTP 413 / payload-size errors in the streaming chat
# loop.  Streamed directly to the user (mirrors ContextOverflowError handling
# in ``agent_chat`` and ``general_chat``) so the chat stream receives a clean
# ``(response, None)`` return instead of a propagated exception.
# This variant is spoken in the assistant's own voice ("I'm sorry, …");
# the error-frame variant composed into "I encountered an error: …" lives in
# ``api/v1/chat._REQUEST_TOO_LARGE_MESSAGE``.  The wording intentionally
# differs to fit each surface, but the actionable guidance (conversation too
# large → start a new one) must stay aligned — enforced by
# ``test_streaming_message_and_error_frame_message_are_consistent``.
REQUEST_TOO_LARGE_STREAMING_MESSAGE = (
    "I'm sorry, the request is too large for the AI model to process. "
    "Your conversation may have grown too long — please start a new conversation. "
    "If the issue persists with a fresh conversation, try reducing the size of any attachments."
)


#: Bound of every free-text field of a failover leg (error text, error code).
_LEG_TEXT_LIMIT = 200
_LEG_CODE_LIMIT = 64


def _bounded(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _leg_error_code(exc: BaseException) -> str | None:
    """The short error code a provider SDK error carries (``rate_limit_exceeded``).

    A 400 that names a refused parameter and no code reads ``<param> not
    supported`` (``temperature not supported``), not the bare error type.
    """
    code = getattr(exc, "code", None)
    param = getattr(exc, "param", None)
    if not code and isinstance(param, str) and param and "Unsupported parameter" in str(exc):
        return f"{param} not supported"
    for attr in ("code", "type"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            return value
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        nested = body.get("error")
        source = nested if isinstance(nested, dict) else body
        for key in ("code", "type"):
            value = source.get(key)
            if isinstance(value, str) and value and value != "error":
                return value
    return None


@dataclass(frozen=True, slots=True)
class FailoverLeg:
    """One target of an exhausted failover chain and why it failed.

    Every text field is secret-redacted and bounded: a leg reaches the run
    error, the trace metadata and the platform chat API body.
    """

    provider: str
    config: str | None
    model: str | None
    #: ``declined``, ``http_error``, ``timeout``, ``connection`` or ``other``.
    kind: str
    category: str | None = None
    status: int | None = None
    code: str | None = None
    error: str | None = None
    #: The model the call asked for when the leg was sent to another one (a
    #: primary routed to its decline fallback earlier in the run).
    requested: str | None = None

    @classmethod
    def of(
        cls,
        provider: str,
        exc: BaseException,
        *,
        config: str | None = None,
        model: str | None = None,
        requested: str | None = None,
    ) -> FailoverLeg:
        from app.core.redaction import redact_secrets
        from app.services.failure_causes import exception_status_code

        def clean(text: str | None, limit: int) -> str | None:
            return _bounded(redact_secrets(text), limit) if text else None

        error = clean(format_exception_message(exc), _LEG_TEXT_LIMIT)
        if isinstance(exc, ModelDeclinedError):
            return cls(
                provider,
                config,
                model or exc.model,
                "declined",
                category=clean(exc.category, _LEG_CODE_LIMIT),
                error=error,
                requested=requested,
            )
        status = exception_status_code(exc)
        name = type(exc).__name__.lower()
        if status is not None:
            kind = "http_error"
        elif isinstance(exc, TimeoutError) or "timeout" in name:
            kind = "timeout"
        elif isinstance(exc, ConnectionError) or "connect" in name or "transport" in name:
            kind = "connection"
        else:
            kind = "other"
        return cls(
            provider,
            config,
            model,
            kind,
            status=status,
            code=clean(_leg_error_code(exc), _LEG_CODE_LIMIT),
            error=error,
            requested=requested,
        )

    @property
    def label(self) -> str:
        """The model of the leg; ``routed (for asked)`` when it served another model's call."""
        label = self.model or self.config or self.provider
        if self.requested and self.requested != self.model:
            return f"{label} (for {self.requested})"
        return label

    def outcome(self) -> str:
        """``declined (cyber)``, ``HTTP 429 rate_limit_exceeded``, ``timeout``..."""
        if self.kind == "declined":
            return f"declined ({self.category})" if self.category else "declined"
        if self.kind == "http_error":
            return f"HTTP {self.status} {self.code}" if self.code else f"HTTP {self.status}"
        if self.kind in ("timeout", "connection"):
            return "timed out" if self.kind == "timeout" else "connection failed"
        return _bounded(self.error or "error", 80)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "config": self.config,
            "model": self.model,
            "kind": self.kind,
            "category": self.category,
            "status": self.status,
            "code": self.code,
            "error": self.error,
            "requested": self.requested,
        }


class AllProvidersFailedError(Exception):
    """Raised when the primary provider and all fallbacks fail.

    *tried* names, per failure and in the same order, the ``(config, model)``
    of the target that failed; :attr:`legs` reads why each one failed.
    """

    def __init__(
        self,
        failures: list[tuple[str, Exception]],
        *,
        skipped: list[tuple[str, str]] | None = None,
        tried: list[tuple[str | None, ...]] | None = None,
    ):
        self.failures = failures
        # ``(provider, model)`` failover targets left out because the model
        # cannot chat. The skip is logged once per process, so the
        # error itself has to say it.
        self.skipped = list(skipped or [])
        targets = list(tried or [])
        self.legs: tuple[FailoverLeg, ...] = tuple(
            FailoverLeg.of(
                name,
                exc,
                config=targets[i][0] if i < len(targets) else None,
                model=targets[i][1] if i < len(targets) else None,
                requested=targets[i][2] if i < len(targets) and len(targets[i]) > 2 else None,
            )
            for i, (name, exc) in enumerate(failures)
        )
        # Every leg before the last declined the content: another attempt
        # asks the same models the same thing, so a retry cannot help.
        self.content_declined = any(leg.kind == "declined" for leg in self.legs) and all(
            leg.kind == "declined" for leg in self.legs[:-1]
        )
        # Every leg refused the input itself (an image too large): what the refusal said.
        self.input_rejected = _chain_input_rejection(failures)
        providers = ", ".join(name for name, _ in failures)
        clauses = "; ".join(f"{leg.label}: {leg.outcome()}" for leg in self.legs)
        message = f"All configured AI providers failed ({providers})"
        message += f": {clauses}. " if clauses else ". "
        if self.content_declined:
            message += (
                "The content was declined; retrying the same content will not help: "
                "rephrase it, or ask your administrator for a fallback model that accepts it."
            )
        elif self.input_rejected:
            message += (
                f"Every model refused the input ({self.input_rejected}); "
                "retrying the same input will not help: change it and send it again."
            )
        else:
            message += "Please try again — if the issue persists, contact your administrator."
        # Two legs on one provider share its outage and its account: say there
        # was no real fallback, or the chain reads as if it had one.
        self.single_provider = len(failures) > 1 and len({name for name, _ in failures}) == 1
        if self.single_provider:
            message += (
                f" The failover chain held one provider only ({failures[0][0]}): "
                "configure a fallback on another provider in Settings -> AI Models."
            )
        if self.skipped:
            targets = ", ".join(f"'{name}' (model={model})" for name, model in self.skipped)
            message += (
                f" Failover skipped {targets}: "
                f"{'not a chat model' if len(self.skipped) == 1 else 'not chat models'}. "
                "Pick a chat model in Settings -> AI Models."
            )
        super().__init__(message)


def is_request_too_large_error(exc: Exception) -> bool:
    """Return True when *exc* is an HTTP 413 / payload-too-large provider error.

    Single source of truth for 413 detection, shared by the graceful chat
    handlers (``agent_chat`` / ``general_chat`` / ``public_chat``), the
    assignment engine, and the web chat's error mapper
    (``api/v1/chat._friendly_error_message``) so classification can never
    drift between surfaces.

    Handles both shapes a 413 can arrive in:

    * the **raw provider exception** — the primary hit the 413 directly and
      ``FailoverLLMProvider`` re-raised it as non-failoverable, and
    * an :class:`AllProvidersFailedError` **whose chain ENDED on a size
      error** — the primary failed transiently (which is why failover
      started), then a fallback hit the 413 that short-circuited the rest of
      the chain.  ``str()`` of the wrapper contains none of the
      keywords, so callers matching on the message alone would miss this
      shape entirely.
    """
    if isinstance(exc, AllProvidersFailedError):
        return bool(exc.failures) and _is_size_error(exc.failures[-1][1])
    return _is_size_error(exc)


# A refusal of the input names it; a refused parameter (temperature, a tool
# schema) is configuration, and changing the input would not help.
_INPUT_MEDIUM = re.compile(r"\b(?:image|pdf|document|file|audio|video|attachment)s?\b", re.IGNORECASE)
# 4xx answering something other than the input: credentials, model, timeout, size, throttle.
_NOT_INPUT_STATUSES = frozenset({401, 403, 404, 408, 413, 429})
# The field path a 400 leads with (``messages.0.content.1.image.source.base64.data: ``);
# only a path into the conversation points at the input, not one into ``tools``.
_FIELD_PATH = re.compile(r"^[\w\[\]]+(?:\.[\w\[\]]+)+:\s*")
_INPUT_PATHS = ("messages", "input", "contents")
# OpenAI words a tool schema or refused-parameter 400 without a path: configuration too.
_CONFIG_WORDING = re.compile(r"\btools?\b|\bfunctions?\b|\bschema\b|\bunsupported (?:parameter|value)\b", re.IGNORECASE)
# Gemini's "Please retry or report in ...": retrying the same input cannot help.
_RETRY_ADVICE = re.compile(r"[.\s]*\bplease retry\b.*$", re.IGNORECASE | re.DOTALL)
_NAMED_LIMIT = re.compile(
    r"\d[\d,.]*\s*(?:px|pixels?|[kmg]i?b|bytes|pages?|tokens?|seconds?|minutes?)\b", re.IGNORECASE
)


def input_rejection(exc: BaseException) -> str | None:
    """What the refusal said when every model refused the request's input, else ``None``.

    Takes a raw provider error or an :class:`AllProvidersFailedError` (every leg refused).
    """
    if isinstance(exc, AllProvidersFailedError):
        return exc.input_rejected
    return _refused_input(exc)


def _chain_input_rejection(failures: list[tuple[str, Exception]]) -> str | None:
    reasons = [_refused_input(exc) for _, exc in failures]
    if not reasons or None in reasons:
        return None
    # The wording that names a limit ("8000 pixels") tells the user what to change.
    return next((reason for reason in reasons if _NAMED_LIMIT.search(reason)), reasons[0])


def _refused_input(exc: BaseException) -> str | None:
    """The provider's message when *exc* refused the request's input, else ``None``."""
    from app.core.redaction import redact_secrets

    if isinstance(exc, ModelDeclinedError | ContextOverflowError) or _is_size_error(exc):
        return None
    upstream = gateway_upstream_refusal(exc)
    if upstream is not None:
        status, message = upstream
    else:
        status, message = error_status(exc) or exception_status_code(exc), _provider_message(exc)
    if status is None:
        # Only a stream's error event comes without a status (openai.APIError proper).
        if not isinstance(exc, openai.APIError) or isinstance(
            exc, openai.APIStatusError | openai.APIConnectionError | openai.APIResponseValidationError
        ):
            return None
    elif not 400 <= status < 500 or status in _NOT_INPUT_STATUSES:
        return None
    message = (message or "").strip()
    path = _FIELD_PATH.match(message)
    if path is not None and not path.group(0).startswith(_INPUT_PATHS):
        return None
    if _CONFIG_WORDING.search(message) or not _INPUT_MEDIUM.search(message):
        return None
    reason = _RETRY_ADVICE.sub("", message[path.end() :] if path else message).strip().rstrip(".")
    return _bounded(redact_secrets(reason), _LEG_TEXT_LIMIT) if reason else None


def _provider_message(exc: BaseException) -> str | None:
    """The provider's own message, whole: unwrapped from the JSON Gemini nests it in, or botocore's ``Error``."""
    message = gateway_error_envelope(error_body(exc)).get("message")
    if not isinstance(message, str) or not message:
        response = getattr(exc, "response", None)
        error = response.get("Error") if isinstance(response, Mapping) else None
        message = error.get("Message") if isinstance(error, Mapping) else getattr(exc, "message", None)
    if not isinstance(message, str) or not message:
        return format_exception_message(exc)
    try:
        nested = json.loads(message)
    except (ValueError, RecursionError):
        return message
    text = gateway_error_envelope(nested).get("message")
    return text if isinstance(text, str) and text else message


def _code_of_413(exc: BaseException) -> str | None:
    if exception_status_code(exc) != 413:
        return None
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else None


def _is_size_error(exc: BaseException) -> bool:
    # The code, so detection does not hang on the SDK's rendering of the body; a
    # 413 with another code (Groq's tokens-per-minute limit) is not a size error.
    if _code_of_413(exc) == "request_too_large":
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in REQUEST_TOO_LARGE_KEYWORDS)


def _is_own_body_cap(exc: BaseException, provider: object) -> bool:
    code = getattr(provider, "body_cap_error_code", None)
    return code is not None and _code_of_413(exc) == code


# Lowercased substrings identifying a provider that could not serve the call at
# all: the platform could not reach a model. Sibling of
# ``REQUEST_TOO_LARGE_KEYWORDS`` and used the same way — keyword matching rather
# than SDK ``isinstance`` checks, because the same condition arrives as a
# different exception class from each of the six provider SDKs and importing all
# of them here to classify one failure is worse than matching the text they all
# put in the message.
#
# This list gates the FREE, UNBOUNDED, non-flag-gated credit-back, so its only
# admissible members are conditions where no model answered. Everything a
# provider REPLIES — a 401, a 403, an exhausted account, a rate limit — is
# excluded on purpose, however much it looks like "not the user's fault":
# those are persistent and indefinitely reproducible, so forgiving them without
# a bound would stop the action quota metering entirely for as long as they
# last. That is the free-unlimited-retries outcome the pool's bound exists to
# prevent. Such failures are still credited back — out of the bounded pool,
# which is what "bounded" is for.
_PROVIDER_UNAVAILABLE_KEYWORDS: tuple[str, ...] = (
    # Transport: nothing answered.
    "connection error",
    "connection refused",
    "connection reset",
    "cannot connect",
    "could not connect",
    "connection aborted",
    # Envoy / gateway phrasing in front of a provider that is down.
    "upstream connect error",
    "disconnect/reset before headers",
    "name or service not known",
    "temporary failure in name resolution",
    # Timeouts, in their provider-shaped phrasings only. A bare "timeout"
    # substring matches any exception whose message happens to contain the
    # word — including a tool crash — and would hand it the unbounded path.
    "request timed out",
    "read timeout",
    "connect timeout",
    "connection timed out",
    "deadline exceeded",
    # Provider down / overloaded (502/503/504, Anthropic's 529). No 500: a
    # provider 500 is real, but "internal server error" is also what a
    # PLATFORM bug stringifies to, and the two are indistinguishable here.
    "service unavailable",
    "serviceunavailable",
    "temporarily unavailable",
    "overloaded",
    "bad gateway",
    "gateway timeout",
    "status code: 502",
    "status code: 503",
    "status code: 504",
    "status code: 529",
    "error code: 502",
    "error code: 503",
    "error code: 504",
    "error code: 529",
    # Nothing to call at all — an administrator's problem, and no model was
    # reached. Distinct from a provider that answered "no".
    "no llm provider",
    "no provider configured",
    "not configured",
)

# Substrings that mean "the provider IS reachable and did answer — it just
# refused THIS traffic or THIS account". Checked first, so none of them can
# reach the unbounded path: a rate limit tracks how much the caller is asking
# for, and an exhausted account or a rejected credential is a persistent state
# that would otherwise un-meter the quota for as long as it lasts. All of them
# are still forgiven, through the bounded pool.
#
# No bare "429": it matches "...4290 tokens". That one failed safe (pool-funded)
# but was matching the wrong thing.
_PROVIDER_REFUSED_KEYWORDS: tuple[str, ...] = (
    # Throttling
    "rate limit",
    "rate_limit",
    "too many requests",
    "status code: 429",
    "error code: 429",
    # Credential / authorisation rejected
    "invalid api key",
    "invalid_api_key",
    "authentication_error",
    "authentication error",
    "invalid authentication",
    "unauthorized",
    "permission_denied",
    "status code: 401",
    "error code: 401",
    # The account behind the credential has run dry. Persistent and
    # self-inflicted: every run fails the same way until someone tops it up.
    "insufficient_quota",
    "credit balance",
    "billing",
)


def is_provider_unavailable_error(exc: BaseException) -> bool:
    """True when the LLM could not be called at all — outage or misconfiguration.

    The distinction this draws is a billing one, which is why it lives here
    next to :func:`is_request_too_large_error` rather than at any single call
    site: a run that never reached a model produced nothing, so its agentic
    action is credited back **for free** (outside the per-agent failure pool),
    while every other failure spends a slot of that bounded pool. See
    ``app/services/quota.py`` and ``app/services/agent_failure_pool.py``.

    True for: a transport failure, a timeout, a 502/503/504/529 overload, an
    exhausted failover chain (:class:`AllProvidersFailedError`), and a platform
    with no provider configured at all.

    False for **everything a provider replies**, however blameless the user is:
    a rate limit, a rejected credential, an account out of credit. Those are
    persistent and indefinitely reproducible, so putting them on the unbounded
    path would stop the action quota metering for as long as they last — the
    free-unlimited-retries outcome the pool's bound exists to prevent. They are
    still credited back, out of the bounded pool. Also false for a
    request-size / context error (the payload is the problem, and it reproduces
    on every provider) and for anything else — a tool crash, a bug, a model
    that answered badly.

    Deliberately conservative: a run whose FIRST calls succeeded and which then
    died on a provider outage is also credited back for free. The cost of
    getting it wrong the other way — charging a user for an outage they could
    not see — is worse than occasionally forgiving a partially-completed run.

    Keyword matching is also why this predicate is a floor, not a taxonomy: it
    reads ``str(exc)`` because six provider SDKs raise six different classes,
    but those exceptions carry ``.status_code`` / ``.type`` that we throw away
    by stringifying. Structural classification of failure causes — and
    persisting them, so an install can be asked what is actually failing — lives
    in ``services/failure_causes.py``, which decides no charge; this function
    stays the coarse billing floor.
    """
    # A model refused the input: it answered, even as the gateway's 502.
    if input_rejection(exc) is not None:
        return False
    if isinstance(exc, AllProvidersFailedError):
        # The wrapper's own message names the providers, not the reason. Classify
        # on the LAST failure in the chain, the same shape
        # ``is_request_too_large_error`` reads: an exhausted chain that ended on
        # a size error is a size problem, not an outage.
        if exc.failures:
            return is_provider_unavailable_error(exc.failures[-1][1])
        return True
    if is_request_too_large_error(exc) if isinstance(exc, Exception) else False:
        return False
    msg = str(exc).lower()
    if any(kw in msg for kw in _PROVIDER_REFUSED_KEYWORDS):
        return False
    if any(kw in msg for kw in _PROVIDER_UNAVAILABLE_KEYWORDS):
        return True
    # Transport errors whose message can be empty (a bare ``TimeoutError``
    # carries no text at all, so the keyword pass above sees nothing).
    return isinstance(
        exc, ConnectionError | TimeoutError | asyncio.TimeoutError | OSError
    )


class EmbeddingConfigurationError(ValueError):
    """Raised when no embedding-capable provider/model is configured."""


PROVIDER_MAP = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "openrouter": OpenRouterProvider,
    "ollama": OllamaProvider,
    "mistral": MistralProvider,
    "xai": XAIProvider,
    "azure_openai": AzureOpenAIProvider,
    "bedrock": BedrockProvider,
    "litellm": LiteLLMProvider,
    "custom_openai": CustomOpenAIProvider,
    "tei": TEIEmbeddingProvider,
    "filigran_gateway": FiligranGatewayProvider,
    "elevenlabs": ElevenLabsProvider,
    "kling": KlingProvider,
    "seedance": SeedanceProvider,
    "runway": RunwayProvider,
    "vertex_ai": VertexAIProvider,
    # Keyed by its own name: an integration of that name feeds it (``integration_fed_only``).
    LMStudioProvider.provider_name: LMStudioProvider,
}

# Providers that serve ONE capability and never chat **by design** — an
# embedding server, a voice or video generator (``only_capability``; ``chat()`` raises
# ``NotImplementedError``). They must be excluded from implicit chat-provider
# selection and failover, and the "exposes no chat-capable model" warning is
# suppressed for them: their catalog is *expected* to contain no chat model,
# so warning would be a false positive. They stay fully usable for their capability and selectable by an
# explicit ``provider_override``.
CHATLESS_PROVIDERS: frozenset[str] = frozenset(
    name for name, cls in PROVIDER_MAP.items() if getattr(cls, "only_capability", None)
)
# The embedding-only ones among them, usable for embeddings via
# :func:`get_embedding_provider` with no model listed.
EMBEDDING_ONLY_PROVIDERS: frozenset[str] = frozenset(
    name for name, cls in PROVIDER_MAP.items() if getattr(cls, "only_capability", None) == "embedding"
)

# Model-name prefix → provider mapping for inferring provider from model id
_MODEL_PROVIDER_PREFIXES: list[tuple[tuple[str, ...], str]] = [
    (("gpt-", "o1-", "o3-", "o4-", "chatgpt-"), "openai"),
    (("claude-",), "anthropic"),
    (("gemini-",), "gemini"),
    (("mistral-", "open-mistral-", "codestral-", "pixtral-"), "mistral"),
    (("grok-",), "xai"),
    # OpenRouter uses org/model format
    (("anthropic/", "openai/", "google/", "meta-llama/", "mistralai/", "x-ai/"), "openrouter"),
    # Bedrock uses provider.model format
    (("anthropic.", "meta.", "mistral.", "amazon.", "cohere."), "bedrock"),
]


def _infer_provider_from_model(model_id: str) -> str | None:
    """Infer the provider name from a model identifier prefix."""
    for prefixes, provider in _MODEL_PROVIDER_PREFIXES:
        if model_id.startswith(prefixes):
            return provider
    return None


# Substrings that identify a NON-chat model — image generation, embeddings,
# audio (speech/transcription) and moderation. Such a model must NEVER be
# selected as a provider's chat default, seeded as the platform/GA chat
# default, or resolved as the chat model for a turn: calling
# ``chat.completions`` with e.g. ``gpt-image-1`` fails on the provider (OpenAI
# returns a 500/400) and surfaces as the confusing "my default model is an
# image model" symptom on installs whose only configured OpenAI model is the
# image model used for the Image Generation / Embeddings sections.
# Matched case-insensitively as substrings of the model id.
_NON_CHAT_MODEL_MARKERS: tuple[str, ...] = (
    "image",  # gpt-image-1, gpt-image-2, *-image-* (also catches "imagen")
    "dall-e",  # dall-e-2 / dall-e-3
    "embed",  # text-embedding-3-*, mistral-embed, nomic-embed-text, titan-embed
    "rerank",  # jina-reranker-*, cohere rerank-*
    "whisper",  # speech-to-text
    "tts",  # text-to-speech
    "transcribe",  # gpt-4o-transcribe
    "moderation",  # omni-moderation-*
    "stable-diffusion",
)

# Authoritative catalog (LiteLLM model-prices map) used to catch non-chat models
# whose id carries no marker substring (e.g. ``databricks-bge-large-en`` =
# embedding, ``jina-reranker-...`` = rerank). The chat-capable modes below are
# the complement that must never be down-classified.
_MODEL_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "seed_data"
    / "model_prices_and_context_window.json"
)
_NON_CHAT_CATALOG_MODES: frozenset[str] = frozenset(
    {
        "embedding",
        "rerank",
        "image_generation",
        "image_edit",
        "audio_transcription",
        "audio_speech",
        "video_generation",
        "search",
        "ocr",
        "moderation",
        "vector_store",
    }
)

# Catalog modes that generate text but are NOT servable through a chat
# completions surface. Legacy ``completion`` models (``gpt-3.5-turbo-instruct``,
# ``babbage-002``, ``davinci-002``) 404 on OpenAI's ``/v1/chat/completions``
# with "This is not a chat model ... Did you mean to use v1/completions?" —
# which is exactly what happened when failover picked one as the OpenAI
# provider's chat default. ``realtime`` models are websocket audio-only.
# ``responses`` models (``gpt-5.2-pro``, ``o3-pro``, the ``-codex`` and
# ``-deep-research`` models) are served by OpenAI's Responses API only, which no
# provider here speaks: ``/v1/chat/completions`` answers them with the same
# "This is not a chat model" 404 when failover picks one.
# Matched by EXACT unprefixed catalog key only (see
# :func:`_catalog_chat_classification`): gateway-prefixed completion entries
# (``ollama/mistral``, ``azure/gpt-3.5-turbo-instruct-0914``, ``sagemaker/...``)
# are per-gateway routing facts — a prefixed id only occurs when routing
# through a translating gateway (LiteLLM prompt-adapts completion-mode models
# to its chat surface), so excluding them would create false positives. An
# unprefixed key, by contrast, means the id is called directly on the vendor's
# ``/v1/chat/completions`` where it 404s. Deprecated completion-only models
# that have dropped OUT of the catalog entirely (and no longer exist
# server-side on ANY route) are covered by
# :data:`_LEGACY_COMPLETION_ONLY_MODELS`, which does strip gateway prefixes.
_COMPLETION_ONLY_CATALOG_MODES: frozenset[str] = frozenset({"completion", "realtime", "responses"})

# Legacy OpenAI completion-only model names that have been deprecated/removed
# from recent LiteLLM catalogs but may still appear in stored provider configs.
# These models predate the chat-completions API and will always 404 on
# ``/v1/chat/completions`` with "This is not a chat model".
# The check in :func:`is_chat_capable_model` strips any gateway prefix first
# so ``openai/text-davinci-003`` is caught the same as ``text-davinci-003``.
_LEGACY_COMPLETION_ONLY_MODELS: frozenset[str] = frozenset(
    {
        "text-davinci-001",
        "text-davinci-002",
        "text-davinci-003",
        "text-curie-001",
        "text-babbage-001",
        "text-ada-001",
        "code-davinci-001",
        "code-davinci-002",
        "code-cushman-001",
        "code-cushman-002",
    }
)


@lru_cache(maxsize=1)
def _catalog_chat_classification() -> tuple[frozenset[str], frozenset[str]]:
    """Classify catalog model ids as non-chat for chat-capability checks.

    Returns ``(non_chat_ids, completion_only_keys)``:

    * ``non_chat_ids`` — lowercased ids (full key + basename) whose catalog
      ``mode`` is in :data:`_NON_CHAT_CATALOG_MODES`. Any id/basename that
      also appears under a chat-capable mode anywhere in the catalog is
      subtracted, so a model the catalog knows can chat is never wrongly
      excluded.
    * ``completion_only_keys`` — lowercased EXACT catalog keys (no gateway
      prefix) whose ``mode`` is in :data:`_COMPLETION_ONLY_CATALOG_MODES`.
      Matched against the full queried id only, and deliberately NOT
      protected by the chat-alias subtraction above: an exact key like
      ``gpt-3.5-turbo-instruct`` must stay excluded even though a gateway
      re-exports the same basename as chat
      (``vercel_ai_gateway/openai/gpt-3.5-turbo-instruct``).

    Fails open (empty sets) if the catalog is missing/unreadable.
    """
    try:
        data = json.loads(_MODEL_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(), frozenset()
    chat: set[str] = set()
    non_chat: set[str] = set()
    completion_only: set[str] = set()
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        key_lower = key.lower()
        ids = {key_lower, key.rsplit("/", 1)[-1].lower()}
        mode = val.get("mode")
        if mode in _NON_CHAT_CATALOG_MODES:
            non_chat |= ids
        elif mode in _COMPLETION_ONLY_CATALOG_MODES:
            # Excluded from the ``chat`` protection set (these entries cannot
            # chat), but only unprefixed keys become exact exclusions.
            if "/" not in key:
                completion_only.add(key_lower)
        else:
            chat |= ids
    return frozenset(non_chat - chat), frozenset(completion_only)


def is_chat_capable_model(model_id: str | None) -> bool:
    """Best-effort check that *model_id* is a chat/completion-capable model.

    Returns ``False`` for image-generation, embedding, audio, rerank,
    moderation, legacy completion-only and Responses-API-only models so they
    are never picked as a
    provider's chat default, seeded as the platform/General Assistant chat
    default, or resolved as a turn's chat model. Detection is three-tier:

    1. :data:`_LEGACY_COMPLETION_ONLY_MODELS` — a static set of deprecated
       OpenAI completion-only model names that have dropped out of current
       LiteLLM catalogs (``text-davinci-003``, ``code-davinci-002``, etc.).
       Checked by stripping any gateway prefix first so
       ``openrouter/text-davinci-003`` is caught the same as
       ``text-davinci-003``.
    2. Authoritative catalog ``mode`` (:func:`_catalog_chat_classification`,
       which catches markerless ids like ``databricks-bge-large-en`` and
       completion-only ids like ``gpt-3.5-turbo-instruct``).  The
       ``completion_only`` set is matched by EXACT unprefixed catalog key only
       (gateway-prefixed entries like ``ollama/mistral`` are routing artifacts
       — the same basename often chats fine through a translating proxy, so
       basename-level generalization would create false positives for ids like
       ``vercel_ai_gateway/openai/gpt-3.5-turbo-instruct`` which a gateway
       re-exports as chat-capable).
    3. :data:`_NON_CHAT_MODEL_MARKERS` substring heuristic for ids absent from
       the catalog.

    Errs towards ``True`` for any unknown id so a newly released chat model is
    never wrongly excluded.

    This is the single source of truth shared by :func:`build_provider`,
    :func:`get_llm_for_user`, :func:`validate_selectable_chat_model` and the
    settings auto-seeding helper.
    """
    if not model_id:
        return False
    mid = model_id.lower()
    basename = mid.rsplit("/", 1)[-1]
    # Tier 1: static legacy completion-only models not in current LiteLLM
    # catalogs — deprecated OpenAI models that predate the chat-completions API.
    if basename in _LEGACY_COMPLETION_ONLY_MODELS:
        return False
    catalog_non_chat, completion_only = _catalog_chat_classification()
    # Tier 2: catalog-based checks.
    # completion_only: EXACT unprefixed key match only (gateway-prefixed variants
    # like ``vercel_ai_gateway/openai/gpt-3.5-turbo-instruct`` are intentionally
    # left as True — translating proxies can make these chat-capable).
    if mid in completion_only:
        return False
    # catalog_non_chat: exact match or basename match (embedding / image / audio
    # models retain their non-chat classification even when gateway-prefixed,
    # because no proxy translates an embedding model into a chat endpoint).
    if mid in catalog_non_chat or basename in catalog_non_chat:
        return False
    # Tier 3: substring markers, and the video and music families
    # a catalog does not list, or lists as chat for their text output.
    if basename.startswith(_VIDEO_MODEL_PREFIXES + _MUSIC_MODEL_PREFIXES):
        return False
    return not any(marker in mid for marker in _NON_CHAT_MODEL_MARKERS)


# Warm the catalog cache at import so the one-time disk read/JSON parse happens
# at startup rather than blocking the event loop on the first request path
# (is_chat_capable_model runs in get_llm_for_user and settings endpoints).
_catalog_chat_classification()


# Sentinel "oldest possible" timestamp for configs that carry no ``created_at``
# (lightweight test stubs). Keeps the recency sort total and aware/naive-safe.
_CONFIG_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _config_recency_key(cfg: LLMProviderConfig) -> tuple[datetime, str]:
    """Deterministic, newest-first sort key for a provider config.

    Several configs can legitimately exist for the same provider — the data
    model has no unique constraint on ``provider`` (e.g. an env-seeded
    read-only row plus a UI-created one). Callers load configs via an unordered
    ``select(LLMProviderConfig)``, so a stable tiebreaker is required to keep
    owner resolution deterministic. ``created_at`` (most recent first, then
    ``id``) mirrors the historical ``order_by(created_at.desc())`` used by the
    env seeder.
    """
    ts = getattr(cfg, "created_at", None)
    if ts is None:
        ts = _CONFIG_EPOCH
    elif ts.tzinfo is None:  # defensive: never mix aware/naive in the sort
        ts = ts.replace(tzinfo=UTC)
    return (ts, str(getattr(cfg, "id", "")))


def select_provider_configs() -> Select:
    """Every provider config, in the order Settings lists them.

    ``list_llm_providers`` orders by ``(created_at, id)``. An implicit pick -
    "the first chat-capable config", "the newest config of that vendor" - reads
    the same order, or it lands on whichever row the heap returns first and can
    change from one request to the next.
    """
    return select(LLMProviderConfig).order_by(
        LLMProviderConfig.created_at, LLMProviderConfig.id
    )


def newest_config(configs: Sequence[LLMProviderConfig]) -> LLMProviderConfig | None:
    """The config a provider type named without an id resolves to: the newest of
    its type, in :func:`select_provider_configs` order."""
    return configs[-1] if configs else None


def oldest_first(configs: Iterable[LLMProviderConfig]) -> list[LLMProviderConfig]:
    """*configs* in the order :func:`select_provider_configs` returns them.

    For a list that did not come from that query: the same key the owner
    resolution sorts newest-first, read the other way.
    """
    return sorted(configs, key=_config_recency_key)


def _config_can_serve_model(cfg: LLMProviderConfig, model_id: str) -> bool:
    """Whether *cfg* can serve *model_id*, by the same rule the write path uses.

    ``settings.py`` and the env seeder both accept a fallback model that is in no
    catalog, resolved by name prefix alone — so requiring ``available_models``
    membership here would ignore the admin's reference for exactly the setup the
    API documents as supported.
    """
    if model_id in (getattr(cfg, "available_models", None) or []):
        return True
    return _infer_provider_from_model(model_id) == cfg.provider


def _resolve_model_owner_config(
    configs: list[LLMProviderConfig],
    model_id: str | None,
) -> LLMProviderConfig | None:
    """Resolve which provider config owns *model_id* from a preloaded list.

    Single source of truth for "who owns this model" — used by both
    :func:`find_provider_for_model` (when only the provider *name* is
    needed) and :func:`get_llm_for_user` (which needs the full config
    row). The lookup is:

    1. exact membership in some `cfg.available_models` list (admin-curated),
    2. fallback to :func:`_infer_provider_from_model` prefix matching for
       newly-added models that haven't been refreshed into
       `available_models` yet.

    When more than one config could own the model (duplicate rows for the same
    provider — the data model permits this), the most recently created one is
    chosen so the result never depends on the unordered query row order. This
    matters most for the explicit fallback model, whose prefix-inferred owner
    config determines which API key failover uses.

    Returns the matching :class:`LLMProviderConfig` or ``None`` when no
    configured provider owns the model.
    """
    if not model_id:
        return None
    ordered = sorted(configs, key=_config_recency_key, reverse=True)
    for cfg in ordered:
        if model_id in (cfg.available_models or []):
            return cfg
    inferred = _infer_provider_from_model(model_id)
    if inferred is None:
        return None
    return next((cfg for cfg in ordered if cfg.provider == inferred), None)


def choose_provider_config(
    configs: list[LLMProviderConfig],
    provider: str,
    model_id: str | None = None,
) -> LLMProviderConfig | None:
    """The one config of *provider* to serve a request, chosen deterministically.

    Several configs may exist for one provider (the data model has no unique
    constraint), so "the" Anthropic key is a choice: the config listing
    *model_id* in its ``available_models`` (the newest when several do), else
    the default config, else the most recently created one. Unlike
    :func:`_resolve_model_owner_config`, no prefix inference: every candidate
    is of *provider*, so it would only pick the newest and skip the default.
    The choice does not depend on the order of *configs*.
    """
    candidates = [cfg for cfg in configs if cfg.provider == provider]
    if not candidates:
        return None
    listing = [cfg for cfg in candidates if model_id and model_id in (cfg.available_models or [])]
    defaults = [cfg for cfg in candidates if getattr(cfg, "is_default", False)]
    return max(listing or defaults or candidates, key=_config_recency_key)


def resolve_configured_fallback(
    configs: list[LLMProviderConfig],
    platform_settings: object,
    *,
    current_model: str | None,
    routing: ProviderRouting | None = None,
) -> tuple[str | None, LLMProviderConfig | None]:
    """Resolve the admin-configured fallback model and its owning config.

    Single source of truth for the "explicit fallback model" configuration
    surface (``PlatformSettings.fallback_model`` / ``DEFAULT_FALLBACK_MODEL``),
    shared by the two consumers that must never disagree about what the
    fallback *is*:

    * :func:`get_llm_for_user`, which hands it to
      :class:`FailoverLLMProvider` so a **provider failure** retries on it; and
    * the agent loop's per-run budget, which switches a run to it when
      the run's **cost budget** can no longer pay for the primary
      (``app.tools.run_budget.resolve_budget_fallback``).

    Returns ``(model_id, owning_config)``.  Both are ``None`` when nothing is
    configured, when the configured model is the one already in use (a
    same-model fallback is pointless), when it is not chat-capable, or when no
    configured provider owns it — the last two cases being misconfigurations
    that are logged (deduplicated per process per value, since this runs on
    every chat turn / assignment run).

    With *routing* the fallback is resolved for that caller: within
    their usable configs, a provider granted to them first, and a model only
    other teams' providers offer is no fallback for them (not a
    misconfiguration, so not logged).
    """
    configured = getattr(platform_settings, "fallback_model", None)
    if not configured or configured == current_model:
        return None, None
    if routing is not None:
        if routing.is_reserved(configured):
            return None, None
        configs = routing.usable
    # Guard: skip the explicit fallback when the configured model is not
    # chat-capable (completion-only, image, embedding, etc.).  Such a model
    # will 404 on /v1/chat/completions on every attempt, making the
    # redundancy chain non-functional.
    if not is_chat_capable_model(configured):
        if _should_warn_fallback_model(configured):
            logger.warning(
                "Configured fallback model %r is not chat-capable "
                "(completion-only or non-chat model) — the explicit fallback "
                "will be skipped. Update Settings -> AI Models / "
                "DEFAULT_FALLBACK_MODEL to a valid chat model.",
                configured,
            )
        return None, None
    # Prefer the provider config the admin picked when the fallback model was
    # saved. ``_resolve_model_owner_config`` below is the newest-row guess, so
    # without this the failover credentials flip when an unrelated config that
    # also advertises the model is created. A reference that is dangling,
    # or that does not advertise the model, falls through to the guess.
    owner = None
    reference_id = getattr(platform_settings, "fallback_provider_id", None)
    if routing is not None and routing.is_withheld(reference_id):
        reference_id = None
    if reference_id:
        referenced = next((cfg for cfg in configs if cfg.id == reference_id), None)
        if referenced is not None and _config_can_serve_model(referenced, configured):
            owner = referenced
        else:
            # Dangling, or pointing at a config that cannot serve the model. Log
            # it: this is the evidence the no-FK choice on the column exists to
            # preserve, and a silently-flipped failover key is hard to debug.
            if _should_warn_fallback_model(f"ref:{reference_id}"):
                logger.warning(
                    "Configured fallback provider reference %r cannot serve "
                    "fallback model %r (deleted config, or the model is not one "
                    "it offers) — falling back to model-owner resolution, so "
                    "failover may not use the intended credentials.",
                    str(reference_id),
                    configured,
                )
    if owner is None:
        owner = (
            routing.owner_for(configured)
            if routing is not None
            else _resolve_model_owner_config(configs, configured)
        )
    if routing is not None:
        owner = routing.prefer_team(owner, configured)
    if owner is None:
        # Misconfiguration: the configured fallback model isn't owned by any
        # configured provider (not in any available_models and no prefix
        # match). The explicit fallback is silently dropped here — and on a
        # single-provider deployment the failover wrapper won't be created at
        # all — so surface it in logs to make debugging the dead setting easy.
        if _should_warn_fallback_model(configured):
            logger.warning(
                "Configured fallback model %r is not owned by any configured "
                "LLM provider — the explicit fallback will be ignored. Check "
                "Settings -> AI Models / DEFAULT_FALLBACK_MODEL.",
                configured,
            )
        return None, None
    return configured, owner


async def resolve_fallback_model_provider(
    db: AsyncSession,
    *,
    current_model: str | None,
    user_id: object = None,
) -> tuple[BaseLLMProvider, str] | None:
    """Instantiate the provider that owns the configured fallback model.

    The ``(provider, model_id)`` pair a caller needs to actually *place a call*
    on the platform fallback model, rather than merely name it.  Used by the
    per-run budget's model downgrade, which — unlike
    :class:`FailoverLLMProvider` — has to switch the model **before** any
    provider error occurs, so it cannot rely on the failover chain.

    Resolved among the providers *user_id* may use; with no user, the
    unrestricted ones only.

    Returns ``None`` when no usable fallback is configured (see
    :func:`resolve_configured_fallback`) or when its provider cannot be built.
    Never raises: a downgrade that cannot be prepared simply does not happen.
    """
    try:
        from app.api.v1.platform_settings_helpers import get_platform_settings

        configs = list((await db.execute(select_provider_configs())).scalars().all())
        routing = await provider_routing(db, user_id, configs)
        ps = await get_platform_settings(db)
        model_id, owner = resolve_configured_fallback(
            routing.usable, ps, current_model=current_model, routing=routing
        )
        if not model_id or owner is None:
            return None
        return await _cached_provider(owner), model_id
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not prepare the configured fallback model provider (%s) — "
            "budget-driven model downgrade is unavailable for this run",
            format_exception_message(exc),
        )
        return None


async def resolve_model_provider(
    db: AsyncSession,
    model_id: str | None,
    *,
    user_id: object = None,
) -> tuple[BaseLLMProvider, str] | None:
    """Instantiate the provider that owns an explicitly named model.

    The sibling of :func:`resolve_fallback_model_provider` for a model chosen
    per agent or per flow rather than platform-wide: the budget's cost
    downgrade needs a *callable* ``(provider, model_id)`` pair before any
    provider error occurs, and a per-agent fallback model names a model
    directly instead of deferring to ``PlatformSettings.fallback_model``.

    Resolved among the providers *user_id* may use, theirs first; with
    no user, the unrestricted ones only.

    Returns ``None`` when no configured provider owns the model or its
    provider cannot be built. Never raises: a downgrade that cannot be
    prepared simply does not happen, and the run keeps its hard stop.
    """
    if not model_id:
        return None
    try:
        configs = list((await db.execute(select_provider_configs())).scalars().all())
        routing = await provider_routing(db, user_id, configs)
        owner = routing.owner_for(model_id)
        if owner is None:
            return None
        return await _cached_provider(owner), model_id
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not prepare a provider for model %r (%s) — budget-driven "
            "model downgrade is unavailable for this run",
            model_id,
            format_exception_message(exc),
        )
        return None


async def find_provider_for_model(
    db: AsyncSession,
    model_id: str,
) -> str | None:
    """Find which provider owns *model_id* by checking ``available_models``.

    Falls back to prefix-based inference when the model is not found in any
    provider's ``available_models`` list (e.g. newly added models before the
    admin refreshes the model list).
    """
    result = await db.execute(select(LLMProviderConfig))
    configs = list(result.scalars().all())
    owner = _resolve_model_owner_config(configs, model_id)
    if owner is not None:
        return owner.provider
    # ``_resolve_model_owner_config`` already consulted
    # ``_infer_provider_from_model``; reaching here means even the inferred
    # provider has no configured row. Surface the prefix-inferred name so
    # legacy callers that just want "who owns this model name" still get a
    # useful answer when no config exists yet.
    return _infer_provider_from_model(model_id)


def _chat_model_candidates(config: LLMProviderConfig) -> list[str]:
    """``default_model`` first, then the catalog, de-duplicated in order.

    ``default_model`` is almost always also the first ``available_models``
    entry, which otherwise produced a confusing doubled catalog in the
    ``build_provider`` warning (e.g.
    ``catalog=['Qwen/...-Embedding-0.6B', 'Qwen/...-Embedding-0.6B']``).
    """
    candidates: list[str] = []
    default_model = getattr(config, "default_model", None)
    if default_model:
        candidates.append(default_model)
    candidates.extend(m for m in (getattr(config, "available_models", None) or []) if m)
    return list(dict.fromkeys(candidates))


def chat_default_model(config: LLMProviderConfig) -> str | None:
    """The model a model-less chat call on *config* runs on.

    The first chat-capable id of ``default_model`` then the catalog - the
    curated default :func:`build_provider` wires onto the instance - or, for a
    config that lists no model at all, the provider class's built-in default.
    ``None`` when the config lists models but none of them can chat.
    """
    candidates = _chat_model_candidates(config)
    curated = next((m for m in candidates if is_chat_capable_model(m)), None)
    if curated or candidates:
        return curated
    provider_cls = PROVIDER_MAP.get(getattr(config, "provider", ""))
    return getattr(provider_cls, "default_model", None)


def provider_instance(
    provider_cls: type[BaseLLMProvider], api_key: str, config_id: Any = None
) -> BaseLLMProvider:
    """An instance of *provider_cls* on *api_key*, for the saved config *config_id*.

    A provider fed only by an integration (``integration_fed_only``) reads it
    through its config, so it is built with the config's id; with none (a
    provider not saved yet) it refuses every request.
    """
    if getattr(provider_cls, "integration_fed_only", False):
        return provider_cls(api_key=api_key, provider_config_id=config_id)  # type: ignore[call-arg]
    return provider_cls(api_key=api_key)


def build_provider(config: LLMProviderConfig, *, api_key: str | None = None) -> BaseLLMProvider:
    """Instantiate a provider from a platform config row.

    *api_key* is the key :func:`_cached_provider` resolved (a vault reference
    read); without it the stored key is decrypted, and a reference is
    refused (``decrypt_value``).
    """
    if api_key is None:
        api_key = decrypt_value(config.api_key_enc)
    provider_cls = PROVIDER_MAP.get(config.provider)
    if not provider_cls:
        raise ValueError(f"Unknown LLM provider: {config.provider}")
    provider = provider_instance(provider_cls, api_key, config.id)
    # Instances are cached one per config (``_get_cached_provider``), so the
    # identity of the row is safe to carry on the instance.
    provider.provider_config_id, provider.provider_config_label = config_identity(config)
    # Wire the CURATED default model onto the instance.  Every provider
    # class ships a hardcoded ``default_model`` fallback for ``model=None``
    # calls — and hardcoded model ids rot: when the vendor retires the
    # snapshot, every model-less call (memory consolidation, fact
    # extraction, prompt generation, style learning — anything routed via
    # ``get_llm_provider`` + ``llm.chat()`` without a model) starts 404ing
    # even though the deployment is configured with perfectly valid models
    # (``chat failed (model=None): 404 model: claude-sonnet-4-20250514``
    # on an instance whose catalog only contained current models).  The
    # config row's ``default_model`` (env-seeded to the first
    # ``LLM_PROVIDER_*_MODELS`` entry, or admin-set) — falling back to the
    # first ``available_models`` entry — keeps those calls on the curated
    # catalog; the class attribute remains only a last resort for rows
    # with no catalog at all.
    #
    # The chat default must never be an image / embedding / audio model: a
    # provider configured only for image generation or embeddings (e.g. an
    # OpenAI row whose ``available_models`` is just ``["gpt-image-1"]`` because
    # it backs the Image Generation / Embeddings sections) would otherwise have
    # its CHAT default set to that non-chat model, so every model-less
    # ``llm.chat()`` call and every failover to this provider would hit
    # ``chat.completions`` with an image model and 500. Walk the
    # candidates in priority order (config default first, then catalog order)
    # and pick the first chat-capable one; fall back to the class chat default
    # when the provider exposes no chat model at all. Keep in step with
    # ``chat_default_model``, which names this model before any instance exists.
    candidate_models = _chat_model_candidates(config)
    curated_default = next(
        (m for m in candidate_models if is_chat_capable_model(m)), None
    )
    if curated_default:
        provider.default_model = curated_default
    elif candidate_models and config.provider not in CHATLESS_PROVIDERS:
        # Embedding-only providers (e.g. ``tei``) are *expected* to expose only
        # embedding models — warning about a missing chat model for them is a
        # false positive. Genuine chat providers with no chat-capable
        # model stay loud so the misconfiguration is visible — but only ONCE per
        # config per process: ``build_provider`` runs on every cache rebuild
        # (every _PROVIDER_CACHE_TTL seconds), and an OpenAI provider used only
        # for Image Generation / Embeddings would otherwise re-log this every
        # ~2 minutes forever.
        warn_key = f"{config.provider}:{getattr(config, 'id', '')}"
        # Check + add under the dedupe lock (build_provider runs outside the
        # provider cache lock and may run concurrently), then log OUTSIDE the
        # lock so exactly one caller emits the warning without holding the lock
        # across the logging call.
        with _warned_lock:
            should_warn = warn_key not in _warned_no_chat_model
            if should_warn:
                _warned_no_chat_model.add(warn_key)
        if should_warn:
            logger.warning(
                "LLM provider '%s' exposes no chat-capable model (catalog=%s); "
                "keeping the built-in chat default '%s' for model-less calls. "
                "Add a chat model to this provider in Settings if it should serve "
                "chat traffic.",
                config.provider,
                candidate_models,
                getattr(provider, "default_model", None),
            )
    if config.context_windows:
        provider.context_windows = config.context_windows
    if config.max_output_tokens:
        provider.max_output_tokens_overrides = config.max_output_tokens
    return provider


def validate_fallback_model(
    fallback_model: str | None,
    configs: list[LLMProviderConfig],
) -> list[str]:
    """Warn if the admin-configured explicit fallback model is not chat-capable.

    A non-chat fallback model (completion-only, image, embedding, etc.) will
    404 on every ``/v1/chat/completions`` call — the failover chain provides
    zero actual redundancy in that case.

    Also warns when the model is not owned by any configured provider, which
    means the explicit fallback will be silently ignored at runtime.

    Returns the list of warning strings emitted (each also logged at WARNING)
    so callers/tests can assert on the findings.
    """
    findings: list[str] = []
    if not fallback_model:
        return findings
    if not is_chat_capable_model(fallback_model):
        msg = (
            f"Configured fallback model {fallback_model!r} is not chat-capable "
            "(completion-only or non-chat model). Every failover attempt to this "
            "model will fail with HTTP 404 on /v1/chat/completions, making the "
            "fallback chain non-functional. Update Settings -> AI Models / "
            "DEFAULT_FALLBACK_MODEL to a valid chat model."
        )
        findings.append(msg)
        logger.warning(msg)
        return findings
    owner = _resolve_model_owner_config(configs, fallback_model)
    if owner is None:
        msg = (
            f"Configured fallback model {fallback_model!r} is not owned by any "
            "configured LLM provider (not in any provider's available_models and "
            "no provider-prefix match). The explicit fallback will be silently "
            "ignored at runtime. Add the model to a provider in Settings -> AI "
            "Models, or clear DEFAULT_FALLBACK_MODEL."
        )
        findings.append(msg)
        logger.warning(msg)
    return findings


def validate_chat_provider_configs(configs: list[LLMProviderConfig]) -> list[str]:
    """Warn about chat-router providers that expose no chat-capable model.

    A provider registered in the chat router (i.e. *not* one of the
    providers in :data:`CHATLESS_PROVIDERS`, an embedding server or a voice generator) that lists
    models but none chat-capable is a misconfiguration: every model-less
    ``llm.chat()`` call and every failover to it resolves to a non-chat model
    (image / embedding / audio), so the router silently falls back. Surfacing
    this once at startup — instead of only via the per-build runtime warning in
    :func:`build_provider` — lets operators fix the stored config proactively.

    Embedding-only providers (e.g. ``tei``) are skipped: their catalog is
    *expected* to contain only embedding models, so a chat warning would be a
    false positive.

    Returns the list of warning strings emitted (each also logged at WARNING)
    so callers/tests can assert on the findings.
    """
    findings: list[str] = []
    for cfg in configs:
        if cfg.provider in CHATLESS_PROVIDERS:
            continue
        candidates: list[str] = []
        if cfg.default_model:
            candidates.append(cfg.default_model)
        candidates.extend(m for m in (cfg.available_models or []) if m)
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            # No curated catalog at all — the provider uses its built-in chat
            # default (chat-capable by construction). Nothing to warn about.
            continue
        if any(is_chat_capable_model(m) for m in candidates):
            continue
        msg = (
            f"LLM provider '{cfg.provider}' is registered for chat but exposes "
            f"no chat-capable model (catalog={candidates}). Chat routed here "
            "will silently fall back. Add a chat model in Settings, or remove "
            "the provider if it is embedding-only."
        )
        findings.append(msg)
        logger.warning(msg)
    return findings


def _config_has_chat_capable_model(config: LLMProviderConfig) -> bool:
    """Whether *config* can serve chat at all.

    A config with a curated catalog (``available_models`` / ``default_model``)
    is chat-capable only if at least one of those ids is chat-capable. A config
    with NO curated catalog is chat-capable by construction — it falls back to
    the provider class's built-in chat default. Used to keep implicit chat
    selection / failover away from a provider that exposes only image /
    embedding models (e.g. an OpenAI row backing the Image Generation section),
    which would otherwise route chat to ``gpt-4o`` and 404.
    """
    models = [m for m in (getattr(config, "available_models", None) or []) if m]
    default_model = getattr(config, "default_model", None)
    if default_model:
        models.append(default_model)
    if not models:
        return True
    return any(is_chat_capable_model(m) for m in models)


def implicit_chat_configs(configs: Iterable[LLMProviderConfig]) -> list[LLMProviderConfig]:
    """The configs a chat call nothing pointed at a provider may run on, in *configs* order.

    The chat-capable ones (:func:`_config_has_chat_capable_model`), embedding-
    and media-only providers aside. A config that lists no model at all runs its
    provider's built-in chat model, which nobody offered, so it serves chat only
    while no config lists a model: an install configured with keys alone still
    chats, and a provider configured for its video, speech or music models, with
    no model list, never takes the chat traffic nor the failover of the
    providers whose chat models are offered.
    """
    chat = [c for c in configs if c.provider not in CHATLESS_PROVIDERS and _config_has_chat_capable_model(c)]
    listing = [c for c in chat if _chat_model_candidates(c)]
    return listing or chat


def implicit_chat_config(configs: list[LLMProviderConfig]) -> LLMProviderConfig | None:
    """The config a chat call with no stored choice lands on, *configs* in Settings order.

    The first of :func:`implicit_chat_configs`; with none, the first config
    that is not embedding-only (its provider's built-in chat model is called);
    with none of those either, the first config at all. Shared by
    :func:`get_llm_provider` and the provider-deletion preview, which names
    this landing when no remaining config lists a chat model.
    """
    return (
        next(iter(implicit_chat_configs(configs)), None)
        or next(
            (c for c in configs if c.provider not in CHATLESS_PROVIDERS),
            None,
        )
        or (configs[0] if configs else None)
    )


async def get_llm_provider(
    user_id: str,
    db: AsyncSession,
    provider_override: str | None = None,
    *,
    _all_configs: list[LLMProviderConfig] | None = None,
) -> BaseLLMProvider:
    """Resolve the LLM provider (platform-wide configs).

    Priority:
    1. Explicit ``provider_override`` (e.g. from assignment config or agent config)
    2. The first chat-capable platform provider, in the order Settings lists
       them (oldest first, tie-broken by id - :func:`select_provider_configs`);
       with none, the first non-embedding-only one on its built-in chat model
       (:func:`implicit_chat_config`)

    Only the providers *user_id* may use are considered, the ones granted to
    them first (:mod:`app.llm.provider_access`).

    When *_all_configs* is provided the DB is not queried — used by
    ``get_llm_for_user`` to avoid redundant round-trips; it must already be the
    caller's usable configs, team first, each half in Settings order.
    """
    configs = _all_configs
    if configs is None:
        result = await db.execute(select_provider_configs())
        loaded = list(result.scalars().all())
        routing = await provider_routing(db, user_id, loaded)
        if routing.audience.ai_disabled or (loaded and not routing.usable):
            raise ProviderNotUsableError(routing.no_usable_message())
        configs = routing.team_first

    config: LLMProviderConfig | None = None

    if provider_override:
        config = next((c for c in configs if c.provider == provider_override), None)

    # Implicit "first found" must skip embedding-only providers (e.g. ``tei``):
    # a chat context resolving to one would call ``chat()`` and hit
    # ``NotImplementedError``, or — pre-fix — silently use its embedding model
    # as the chat default. It must ALSO prefer a provider that actually
    # exposes a chat-capable model, so a chat context never implicitly lands on
    # an image/embedding-only OpenAI row (``available_models=['gpt-image-2']``)
    # and route to ``gpt-4o``. Only fall back to *any* configured
    # provider when no chat-capable one exists at all, preserving prior
    # behaviour on an embedding-only install (the chat call then fails with a
    # clear provider error rather than a confusing silent route).
    if not config:
        config = implicit_chat_config(configs)

    if not config:
        raise ValueError(
            "No LLM provider configured. An admin must add an API key in Settings > AI and models > Models."
        )

    return await _cached_provider(config)


async def get_llm_for_user(
    db: AsyncSession,
    user_id: str | None,
    provider_override: str | None = None,
    model_override: str | None = None,
    *,
    provider_id_override: uuid.UUID | None = None,
    failover: bool = True,
) -> tuple[BaseLLMProvider, str | None]:
    """Resolve the LLM provider **and model** for a user context.

    Resolution priority:
    1. Explicit ``model_override`` (e.g. from agent config or GA config)
    2. The agent default model (from ``PlatformSettings.agent_default_model``)
    3. User preference (from ``User.preferred_llm_model``)
    4. Platform default (from ``GeneralAssistantConfig.llm_model``)
    5. Any configured platform provider - the first chat-capable config in the
       order Settings lists them (oldest first, tie-broken by id), else the
       first non-embedding-only one on its built-in chat model

    Every tier resolves for *user_id*, the caller
    (:mod:`app.llm.provider_access`): only among the providers they may use, a
    provider granted to them first, a model only other teams' providers offer
    skipped like a non-chat one, and a stored reference to a provider they may
    not use read as absent. A shared agent therefore runs on each caller's own
    team provider.

    When multiple providers are configured, the returned provider is
    wrapped in a :class:`FailoverLLMProvider` that automatically falls
    back to alternative providers if the primary provider fails after
    exhausting retries. *failover* ``False`` returns the primary alone: a
    caller that must measure the model it names (an evaluation run) gets
    that model's own error instead of another model's answer.

    Returns:
        ``(provider_instance, resolved_model)`` — the model may be ``None``
        which means "use the provider's built-in default".
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.models.general_assistant_config import GeneralAssistantConfig
    from app.models.user import User

    # Normalize an empty-string ``provider_override`` to ``None`` up front:
    # callers (UIs / form submissions) commonly send ``""`` to mean "no
    # override", but ``provider_override is not None`` would still be True
    # for an empty string and inconsistently flip downstream branches
    # (e.g. the explicit-override warning wording). All truthy
    # ``if provider_override:`` checks already treat ``""`` as "no
    # override"; this line just makes the "is the override present?" flag
    # agree with the rest of the resolution logic.
    if not provider_override:
        provider_override = None

    # The AI work of this context is for *user_id*: the calls made later on it
    # with no user of their own (compaction, memory, embeddings) resolve for
    # the same person, so a provider that excludes them stays out.
    bind_acting_user(user_id)

    # ── Single batch: load all provider configs + platform settings + GA
    # config + user preference in as few round-trips as possible. ──
    # Ordered: the implicit pick in ``get_llm_provider`` and the failover chain
    # below both take configs in list order.
    all_configs_result = await db.execute(select_provider_configs())
    loaded_configs = list(all_configs_result.scalars().all())
    # The providers this caller may use: every step below resolves
    # within them, a provider granted to the caller first.
    routing = await provider_routing(db, user_id, loaded_configs)
    # A person whose group switches AI off gets no model even where no provider
    # is configured here and an environment fallback would answer.
    if routing.audience.ai_disabled or (loaded_configs and not routing.usable):
        raise ProviderNotUsableError(routing.no_usable_message())
    all_configs = routing.usable

    ps = await get_platform_settings(db)

    def _accept_chat_model(candidate: str | None) -> str | None:
        """Return *candidate* only when it is a usable chat model.

        A configured chat default (agent override, agent default, user
        preference, GA / platform default) that points at a non-chat model
        (image / embedding / audio / moderation) is a misconfiguration —
        historically these could be persisted (e.g. an OpenAI image-only
        install seeded ``gpt-image-1`` as the platform default before the
        write-path guard existed). Resolving such a value would route the turn
        to ``chat.completions`` with a non-chat model and 500 repeatedly. We
        skip it here (WARNING, so the admin can fix the stored config) and let
        resolution fall through to the next tier — and ultimately to the
        provider's chat default — so chat keeps working regardless of stale
        data.
        """
        if not candidate:
            return None
        if routing.is_reserved(candidate):
            # Only providers restricted to other teams offer it: not this
            # caller's model, the next tier decides.
            logger.debug(
                "Chat model %r is only offered by providers this caller may "
                "not use; skipping it during resolution.",
                candidate,
            )
            return None
        if is_chat_capable_model(candidate):
            return candidate
        logger.warning(
            "Configured chat model %r is not a chat-capable model (image / "
            "embedding / audio / moderation) and cannot be used for chat — "
            "skipping it during resolution. Update the agent / General "
            "Assistant / platform default model in Settings.",
            candidate,
        )
        return None

    # 1. Explicit override (agent / GA config)
    effective_model: str | None = _accept_chat_model(model_override)

    # 2. Agent default from PlatformSettings.agent_default_model
    if not effective_model:
        effective_model = _accept_chat_model(ps.agent_default_model)

    # Bind the LLM caller identity for this context (the gateway provider
    # signs outbound XTM JWTs with it — see app.llm.identity) and load the
    # user's model preference in the same round-trip.
    user_pref: str | None = None
    if user_id:
        try:
            row = (
                await db.execute(
                    select(User.email, User.preferred_llm_model).where(
                        User.id == uuid.UUID(user_id)
                    )
                )
            ).one_or_none()
            if row:
                user_email, user_pref = row
                set_llm_caller(user_id, user_email)
        except (ValueError, TypeError):
            pass  # non-UUID caller (e.g. "anonymous") → service identity
        except Exception as exc:
            # ERROR-level: the user's explicit LLM choice is being
            # silently overridden by the platform default. Surfacing
            # this prevents the "why isn't my model being used?" class
            # of support tickets.
            logger.error(
                "Failed to load user LLM preference for %s — falling back "
                "to platform/agent default: %s",
                user_id,
                format_exception_message(exc),
                exc_info=True,
            )

    # 3. User preference from User.preferred_llm_model
    if not effective_model and user_pref:
        effective_model = _accept_chat_model(user_pref)

    # 4. Platform default from GeneralAssistantConfig.llm_model.
    # The agent default (PlatformSettings.agent_default_model) was already
    # resolved in step 2 — if we reach here it was empty or non-chat, so
    # re-checking it would only repeat work. Fall straight through to the
    # General Assistant's own configured model as the final tier.
    ga_reference_id: uuid.UUID | None = None
    ga_provider_name: str | None = None
    if not effective_model:
        ga_result = await db.execute(select(GeneralAssistantConfig).limit(1))
        ga = ga_result.scalar_one_or_none()
        if ga:
            effective_model = _accept_chat_model(ga.llm_model)
            # The GA's model carries its own provider reference. Without it this
            # tier still resolves the owner newest-config-first, so the plain-user-chat
            # path would call whichever config advertises the model newest, even once
            # the admin stored one.
            if effective_model:
                ga_reference_id = getattr(ga, "llm_provider_id", None)
                ga_provider_name = getattr(ga, "llm_provider", None)

    # A stored reference to a provider this caller may not use is not stale,
    # it is not theirs: read it as absent, without the stale-reference warning.
    if provider_id_override is not None and routing.is_withheld(provider_id_override):
        provider_id_override = None
    if ga_reference_id is not None and routing.is_withheld(ga_reference_id):
        ga_reference_id = None
    agent_default_provider_id = getattr(ps, "agent_default_provider_id", None)
    if agent_default_provider_id is not None and routing.is_withheld(
        agent_default_provider_id
    ):
        agent_default_provider_id = None

    # Resolve which provider owns the model — single source of truth shared
    # with `find_provider_for_model` (scan `available_models`, fall back to
    # prefix inference), a provider granted to the caller first.
    model_owner_config = routing.owner_for(effective_model)

    effective_provider_name: str | None = provider_override
    explicit_provider_override = provider_override is not None
    # ``provider_id_override_resolved`` distinguishes "UUID matched a real
    # configured provider" from "UUID was supplied but stale, so routing
    # actually fell back to ``provider_override`` (name) or to the
    # model-owner provider". The mismatch warning downstream keys on this
    # flag so the UUID-specific message (which logs the UUID) only fires
    # when the UUID actually drove the routing decision — otherwise the
    # log would claim ``override id=<uuid> resolved to '<provider>'``
    # even though the UUID did not in fact resolve.
    provider_id_override_resolved = False
    # True when ``primary_config`` was chosen by a stored provider-config
    # reference (a UUID) rather than by a provider name or the model-owner guess.
    # The mismatch check below keys on it: only a reference identifies a config
    # *instance*, so only a reference can outrank ``model_owner_config``.
    primary_config_from_reference = False
    primary_config: LLMProviderConfig | None = None
    if provider_id_override:
        primary_config = next(
            (cfg for cfg in all_configs if cfg.id == provider_id_override),
            None,
        )
        if primary_config is not None:
            # UUID matched a configured row — keep both override channels
            # aligned so downstream logging / fallbacks are consistent.
            provider_id_override_resolved = True
            primary_config_from_reference = True
            effective_provider_name = primary_config.provider
        elif provider_override:
            # UUID stale (provider row deleted/renamed since the override
            # was persisted) but the caller also supplied a name override.
            # Surface the stale UUID separately from the eventual mismatch
            # warning so operators can scrub the dangling reference;
            # routing itself falls back to the name lookup so the user's
            # intent is still respected as far as possible.
            logger.warning(
                "LLM provider override id=%r did not resolve to a configured "
                "provider (stale UUID); falling back to provider_override=%r.",
                str(provider_id_override),
                provider_override,
            )
            primary_config = next(
                (cfg for cfg in all_configs if cfg.provider == provider_override),
                None,
            )
    elif provider_override:
        primary_config = next(
            (cfg for cfg in all_configs if cfg.provider == provider_override),
            None,
        )
    elif ga_reference_id or ga_provider_name:
        primary_config = next(
            (cfg for cfg in all_configs if cfg.id == ga_reference_id), None
        )
        primary_config_from_reference = primary_config is not None
        if primary_config is None and ga_reference_id:
            # Same stale-reference contract as the explicit override above
            # (FR-007): surface the dangling id rather than silently dropping to
            # the model-owner guess with no record of why. Logged whether or not
            # a name was stored — the endpoint writes the two independently, and
            # a dangling id with no name is the case with the least evidence.
            logger.warning(
                "General Assistant provider reference id=%r did not resolve to "
                "a configured provider (stale UUID); falling back to %s.",
                str(ga_reference_id),
                (
                    f"its stored provider name {ga_provider_name!r}"
                    if ga_provider_name
                    else "the model owner"
                ),
            )
        if primary_config is None and ga_provider_name:
            primary_config = next(
                (
                    cfg
                    for cfg in sorted(
                        all_configs, key=_config_recency_key, reverse=True
                    )
                    if cfg.provider == ga_provider_name
                ),
                None,
            )
    elif agent_default_provider_id:
        primary_config = next(
            (cfg for cfg in all_configs if cfg.id == agent_default_provider_id),
            None,
        )
        # A reference only speaks for the model it was saved with. This one was
        # saved with ``agent_default_model``, so it must not outrank the model
        # owner for a model that came from an agent override or a user
        # preference — that would move someone else's chosen model onto the
        # platform default's credentials and billing.
        primary_config_from_reference = primary_config is not None and (
            effective_model is not None
            and effective_model == getattr(ps, "agent_default_model", None)
        )
    elif model_owner_config:
        primary_config = model_owner_config

    # Stale-override fallback. If every override we tried above (UUID,
    # name, platform default) failed to resolve to a configured provider,
    # the if/elif chain leaves `primary_config = None`. Without this
    # final fallback, the downstream `get_llm_provider()` call would just
    # pick the first configured provider — which would then race against
    # `effective_model` and reintroduce the invalid provider/model pair
    # this PR is meant to prevent. Falling back to the model owner here
    # keeps the routing decision model-aware regardless of how the
    # override resolution failed.
    if primary_config is None and model_owner_config is not None:
        if explicit_provider_override or provider_id_override:
            logger.warning(
                "LLM provider override (name=%r, id=%r) did not resolve to a "
                "configured provider. Falling back to the model owner '%s' "
                "for model '%s' to avoid an invalid provider/model pair.",
                provider_override,
                str(provider_id_override) if provider_id_override else None,
                model_owner_config.provider,
                effective_model,
            )
        primary_config = model_owner_config

    # A stored reference that advertises the model is not a mismatch, even when a
    # differently-named provider also advertises it: ``model_owner_config`` is the
    # newest-row guess, so auto-correcting against it would throw away the config
    # the admin explicitly picked — a gateway fronting a vendor beside that
    # vendor's own key. A reference that CANNOT serve the model still
    # auto-corrects below, because calling it would 404 on the provider.
    reference_serves_model = bool(
        primary_config_from_reference
        and effective_model
        and primary_config is not None
        and _config_can_serve_model(primary_config, effective_model)
    )

    if (
        primary_config
        and model_owner_config
        and primary_config.provider != model_owner_config.provider
        and not reference_serves_model
    ):
        # Three distinct cases produce different log messages so operators
        # can tell from a single line *why* the routing diverged. All three
        # auto-correct to the model owner — only the wording differs:
        #
        # 1. Explicit override by UUID that actually resolved
        #    (``provider_id_override_resolved`` is True) — the caller
        #    picked a specific provider row. The UUID is logged so a
        #    "why did my hand-tuned override get ignored?" debug is one
        #    grep away. We deliberately do NOT key on
        #    ``provider_id_override`` here: when the UUID was supplied
        #    but stale, the actual provider was driven by the name
        #    override (or the model-owner fallback), so logging
        #    ``override id=<uuid> resolved to …`` would be a lie.
        # 2. Explicit override by name (``provider_override``) — same
        #    intent but identified by provider key (``anthropic`` /
        #    ``openai`` etc.) instead of UUID. Also fires when the UUID
        #    was stale and the name override took over.
        # 3. Implicit selection (platform default, or model-owner
        #    fallback applied earlier in this function) — softer wording
        #    because no caller explicitly picked this provider.
        if provider_id_override_resolved:
            logger.warning(
                "LLM provider override id=%r resolved to '%s' but does not match "
                "model '%s' (owned by '%s'). Auto-correcting to the model owner provider.",
                str(provider_id_override),
                primary_config.provider,
                effective_model,
                model_owner_config.provider,
            )
        elif explicit_provider_override:
            logger.warning(
                "Explicit LLM provider override '%s' does not match model '%s' (owned by '%s'). "
                "Auto-correcting to the model owner provider.",
                primary_config.provider,
                effective_model,
                model_owner_config.provider,
            )
        else:
            logger.warning(
                "Resolved LLM provider '%s' does not match model '%s' (owned by '%s'). "
                "Routing to the model owner provider to avoid invalid provider/model pair.",
                primary_config.provider,
                effective_model,
                model_owner_config.provider,
            )
        primary_config = model_owner_config

    # A provider granted to the caller serves the run when it can, even over a
    # stored reference to an unrestricted one: that is what lets a shared agent
    # run on each team's own key.
    primary_config = routing.prefer_team(primary_config, effective_model)

    if primary_config:
        effective_provider_name = primary_config.provider
        provider = await _cached_provider(primary_config)
    else:
        provider = await get_llm_provider(
            user_id or "system",
            db,
            provider_override=effective_provider_name,
            _all_configs=routing.team_first,
        )

    if not failover:
        return provider, effective_model

    # Build failover chain lazily — store configs, build providers on demand.
    # Embedding-only providers (e.g. ``tei``) are excluded: failing a chat call
    # over to one would only raise ``NotImplementedError`` and waste a slot in
    # the chain. Providers that expose only non-chat models (e.g. an
    # OpenAI row whose catalog is just image models) are excluded too: failing
    # over to one would route to its ``gpt-4o`` built-in default and 404,
    # burning a slot and the per-attempt timeout for nothing.
    # A provider listing no model is left out too while another lists chat
    # models (``implicit_chat_configs``). Only the caller's usable configs,
    # theirs first: a run never fails over onto another team's key.
    fallback_configs = [
        c
        for c in implicit_chat_configs(routing.team_first)
        if c.id != (primary_config.id if primary_config else None)
        and c.provider != (effective_provider_name or provider.provider_name)
    ]

    # Admin-configured explicit fallback model (Settings -> AI Models /
    # ``DEFAULT_FALLBACK_MODEL``). Resolve its owning provider so failover can
    # try this exact model first when the primary fails — even when it is owned
    # by the *same* provider as the primary (a same-provider model fallback),
    # which is why the wrapper is also created when there is no separate
    # fallback provider chain.
    configured_fallback_model, fallback_model_config = resolve_configured_fallback(
        all_configs, ps, current_model=effective_model, routing=routing
    )

    # A model whose declines another model of the same provider answers
    # (``decline_fallback_models``) needs the wrapper even with no other
    # provider configured.
    if (
        fallback_configs
        or fallback_model_config is not None
        or decline_fallback_models(effective_model or getattr(provider, "default_model", None))
    ):
        # The chain is built lazily and synchronously: a key in a vault is read now.
        prepared = await warm_referenced_providers(
            [*fallback_configs, *([fallback_model_config] if fallback_model_config is not None else [])]
        )
        provider = FailoverLLMProvider(
            primary=provider,
            fallback_configs=fallback_configs,
            fallback_model=(
                configured_fallback_model
                if fallback_model_config is not None
                else None
            ),
            fallback_model_config=fallback_model_config,
            prepared=prepared,
        )

    return provider, effective_model


# ── Failover LLM Provider ───────────────────────────────────────────────────


class _WatchedTokens:
    """One attempt's ``on_token``, watched for output that reached the caller.

    The attempt is also named on the trace from its first token
    (*on_first_token*): from there it is serving the call, even if the stream
    then errors, times out or is cancelled. Only ``on_token`` is
    wrapped: an ``on_thinking`` sink is read by its type (``reads_reasoning``).
    """

    def __init__(
        self,
        on_token: Callable[[str], Awaitable[None]],
        on_first_token: Callable[[], None],
    ) -> None:
        self._on_token = on_token
        self._on_first_token = on_first_token
        self.emitted = False

    async def __call__(self, token: str) -> None:
        if not self.emitted:
            self.emitted = True
            self._on_first_token()
        await self._on_token(token)


#: Bounds of the jittered pause before a failover leg on the provider that
#: just failed. Each provider already retries its own call; this spaces the
#: second leg from the first so it does not land in the same overload window.
SAME_PROVIDER_FAILOVER_BACKOFF_SECONDS = (0.5, 2.0)


async def _same_provider_backoff() -> None:
    low, high = SAME_PROVIDER_FAILOVER_BACKOFF_SECONDS
    await asyncio.sleep(random.uniform(low, high))  # noqa: S311 - jitter, not crypto


class FailoverLLMProvider(BaseLLMProvider):
    """LLM provider wrapper that fails over to alternative providers.

    When the primary provider's ``chat()`` call raises an exception (after
    its own retry logic is exhausted), this wrapper tries each fallback
    provider in order.  The model is automatically adjusted: if the
    failing model belongs to the primary provider, each fallback uses
    its own default model instead.

    When an admin has configured an explicit fallback model
    (``PlatformSettings.fallback_model`` / ``DEFAULT_FALLBACK_MODEL``), the
    provider that owns that model is tried **first** on failover with that
    exact model — before the generic per-provider chain.  This lets operators
    pin a specific resilient model (e.g. a cheaper / higher-quota model, or a
    different model on the same provider) instead of relying on each fallback
    provider's built-in default.

    Fallback providers are built **lazily** from stored config rows to avoid
    Fernet decrypt + SDK client creation when the primary succeeds (the
    common case).

    This is transparent to all consumers — they interact with the same
    :class:`BaseLLMProvider` interface.

    A call whose output already streamed to the caller is moved to the next
    target only when the caller passed ``on_stream_reset`` and took that
    output back; otherwise the error surfaces, never a second answer
    after the first.
    """

    supports_stream_reset = True
    # ``last_stop_reason`` here is copied from the provider that streamed,
    # which recorded it for the calling task itself.
    relays_stream_stop_reason = True

    def __init__(
        self,
        primary: BaseLLMProvider,
        fallbacks: list[BaseLLMProvider] | None = None,
        fallback_configs: list[LLMProviderConfig] | None = None,
        *,
        fallback_model: str | None = None,
        fallback_model_config: LLMProviderConfig | None = None,
        prepared: Mapping[str, BaseLLMProvider] | None = None,
    ) -> None:
        self.primary = primary
        self._fallbacks = fallbacks
        self._fallback_configs = fallback_configs
        # Instances whose vault-held key was read for this call, by config id
        # (:func:`warm_referenced_providers`): used before the shared cache.
        self._prepared = dict(prepared or {})
        # Admin-configured explicit fallback model (Settings -> AI Models /
        # ``DEFAULT_FALLBACK_MODEL``).  When set, the provider that owns this
        # model is tried first on failover with this exact model — before the
        # generic per-provider chain (which uses each provider's own default
        # model).  ``fallback_model_config`` is the resolved owning config.
        self._fallback_model = fallback_model
        self._fallback_model_config = fallback_model_config
        # Requested model -> the decline fallback that answered for it
        # (:meth:`_targets_after`). Later calls of this wrapper - one agent
        # run - go straight to it instead of being declined again.
        self._decline_routes: dict[str, str] = {}
        self.provider_name = primary.provider_name
        self.provider_config_id = getattr(primary, "provider_config_id", None)
        self.provider_config_label = getattr(primary, "provider_config_label", None)
        self.max_tools = primary.max_tools
        self.context_window = primary.context_window
        self.context_windows = primary.context_windows
        self.max_output_tokens = primary.max_output_tokens
        self.max_output_tokens_overrides = primary.max_output_tokens_overrides
        # Mirror the primary's streaming structured-output capability so
        # callers gating on the wrapper (e.g. ``agent_loop._streaming_chat``)
        # see the real provider's support.  Fallback calls to another
        # provider strip ``structured_output`` via ``_FALLBACK_EXCLUDED_KWARGS``.
        self.supports_streaming_structured_output = getattr(
            primary, "supports_streaming_structured_output", False
        )

    @property
    def has_configured_fallbacks(self) -> bool:
        """True when a cross-provider chain or an admin fallback model is set.

        A wrapper with neither exists only for the primary's own decline
        fallbacks; the LLM proxy dispatches on the underlying provider
        in that case (it handles declines itself and does not fail over), so it
        keeps the Anthropic bridge and raw error surfacing of an unwrapped
        single-provider install.
        """
        return bool(self.fallbacks) or self._fallback_model_config is not None

    @property
    def fallbacks(self) -> list[BaseLLMProvider]:
        """Build fallback providers lazily on first access."""
        if self._fallbacks is None:
            self._fallbacks = []
            for cfg in self._fallback_configs or []:
                try:
                    self._fallbacks.append(self._provider_for(cfg))
                except Exception as exc:
                    logger.debug("Skipping fallback provider %s: %s", cfg.provider, format_exception_message(exc))
        return self._fallbacks

    def _provider_for(self, config: LLMProviderConfig) -> BaseLLMProvider:
        prepared = self._prepared.get(str(config.id))
        return prepared if prepared is not None else _get_cached_provider(config)

    def _iter_fallback_targets(self) -> list[tuple[BaseLLMProvider, str | None]]:
        """The :meth:`_fallback_candidates` that can serve a chat call (:meth:`_can_chat`)."""
        return [t for t in self._fallback_candidates() if self._can_chat(*t)]

    async def max_tools_for(self, model: str | None) -> int:
        return await provider_max_tools(self.primary, self._routed_model(model) or model)

    def _routed_model(self, model: str | None) -> str | None:
        """The decline fallback this wrapper already sent *model*'s calls to."""
        requested = self._target_model(self.primary, model)
        return self._decline_routes.get(requested) if requested else None

    def _targets_after(
        self, exc: Exception, model: str | None, *, routed: str | None = None,
    ) -> tuple[list[tuple[BaseLLMProvider, str | None]], int]:
        """The targets failover tries once the primary failed with *exc*, and
        how many of them, at the head, are decline fallbacks.

        A decline another model may answer (``failover_allowed``) is first
        handed to the models the requested one declares
        (:func:`decline_fallback_models`), on the primary's own provider, in
        order - after *routed*, the one this call was sent to, when it
        was; then comes the usual chain, without a target already tried.
        """
        requested = self._target_model(self.primary, model)
        tried = {self._target_key(self.primary, routed or requested)}
        declined: list[tuple[BaseLLMProvider, str | None]] = []
        if isinstance(exc, ModelDeclinedError) and exc.failover_allowed:
            models = decline_fallback_models(requested)
            if routed in models:
                models = models[models.index(routed) + 1:]
            declined = [
                (self.primary, fallback_model)
                for fallback_model in models
                if fallback_model != requested and self._can_chat(self.primary, fallback_model)
            ]
        tried |= {self._target_key(provider, m) for provider, m in declined}
        rest = [
            (provider, m) for provider, m in self._iter_fallback_targets()
            if self._target_key(provider, m) not in tried
        ]
        return declined + rest, len(declined)

    def _route_declined(self, model: str | None, fallback_model: str | None) -> None:
        """Send *model*'s later calls straight to the decline fallback that answered."""
        requested = self._target_model(self.primary, model)
        if not requested or not fallback_model or self._decline_routes.get(requested) == fallback_model:
            return
        self._decline_routes[requested] = fallback_model
        logger.info(
            "Model '%s' declined; later calls of this run go to %s",
            requested,
            self._describe_target(self.primary, fallback_model),
        )

    def _primary_served_as(
        self, routed: str | None, usage_sink: StreamUsageSink | None = None,
    ) -> Callable[[], None]:
        """What records the primary's attempt as serving: as itself, or as the
        decline fallback it was *routed* to."""
        if routed is None:
            return self._primary_served
        return partial(self._fallback_served, self.primary, routed, usage_sink)

    @classmethod
    def _target_key(cls, provider: BaseLLMProvider, explicit_model: str | None) -> tuple[object, str | None]:
        """What makes two failover targets the same call: config (or instance) and model."""
        config_id = getattr(provider, "provider_config_id", None)
        return (
            str(config_id) if config_id else id(provider),
            cls._target_model(provider, explicit_model),
        )

    def _fallback_candidates(self) -> list[tuple[BaseLLMProvider, str | None]]:
        """Ordered ``(provider, explicit_model)`` pairs failover may try after the primary.

        When an admin has configured an explicit fallback model, the provider
        that owns it is tried first with that exact model (``explicit_model``
        is the model id).  The remaining configured providers follow, each
        using their own default model (``explicit_model`` is ``None``).

        The explicit fallback's provider instance is skipped from the generic
        chain so the same provider isn't retried with its default model right
        after its explicit-model attempt failed.  When no fallback model is
        configured this returns ``[(fb, None) for fb in self.fallbacks]`` —
        byte-for-byte the legacy behaviour.
        """
        targets: list[tuple[BaseLLMProvider, str | None]] = []
        explicit_provider: BaseLLMProvider | None = None
        if self._fallback_model and self._fallback_model_config is not None:
            try:
                explicit_provider = self._provider_for(self._fallback_model_config)
                targets.append((explicit_provider, self._fallback_model))
            except Exception as exc:
                logger.debug("Skipping configured fallback model provider: %s", format_exception_message(exc))
                explicit_provider = None
        for fb in self.fallbacks:
            if explicit_provider is not None and fb is explicit_provider:
                continue
            targets.append((fb, None))
        return targets

    @staticmethod
    def _target_model(provider: BaseLLMProvider, explicit_model: str | None) -> str | None:
        """The model a failover target is actually called with."""
        return explicit_model or getattr(provider, "default_model", None)

    @staticmethod
    def _describe_target(provider: BaseLLMProvider, model: str | None) -> str:
        """``config=<id>, model=<id>`` naming a failover target in a log line."""
        described = f"model={model or '<provider default>'}"
        config_id = getattr(provider, "provider_config_id", None)
        return f"config={config_id}, {described}" if config_id else described

    @classmethod
    def _can_chat(cls, provider: BaseLLMProvider, explicit_model: str | None) -> bool:
        """Whether a failover target may serve a chat call.

        Every failover path is a chat path. The declared capability decides
        first: a model the capability rules classify as non-chat
        (completion-only, embedding, image, audio, rerank, moderation) is
        dropped instead of burning a slot of the chain on a guaranteed refusal.
        Then what the endpoint said: a model its config's endpoint refused as
        non-chat (:func:`is_non_chat_model_error`) stays out for
        ``_REFUSED_CHAT_TARGET_TTL``. A target with no known model keeps its
        provider's built-in chat default and is kept.
        """
        model = cls._target_model(provider, explicit_model)
        if not model:
            return True
        if not is_chat_capable_model(model):
            if _should_warn_fallback_model(f"target:{provider.provider_name}:{model}"):
                logger.warning(
                    "Skipping failover target '%s' (%s): not a chat-capable "
                    "model. Pick a chat model for this provider in Settings -> "
                    "AI Models.",
                    provider.provider_name,
                    cls._describe_target(provider, model),
                )
            return False
        return not _is_refused_chat_target(provider, model)

    @classmethod
    def _record_fallback_failure(
        cls,
        provider: BaseLLMProvider,
        explicit_model: str | None,
        exc: Exception,
    ) -> None:
        """Leave a target out of failover when its endpoint refused the model as non-chat."""
        model = cls._target_model(provider, explicit_model)
        if not model or not is_non_chat_model_error(exc):
            return
        if _remember_refused_chat_target(provider, model):
            logger.warning(
                "LLM provider '%s' (%s) refused a chat call because the model "
                "cannot chat; failover skips it for %d minutes, or until the "
                "LLM settings change. Pick a chat model for this provider in "
                "Settings -> AI Models.",
                provider.provider_name,
                cls._describe_target(provider, model),
                _REFUSED_CHAT_TARGET_TTL // 60,
            )

    def _primary_leg(self, model: str | None, routed: str | None) -> tuple[str | None, ...]:
        """``(config, model, requested)`` of the primary's failed attempt.

        A primary already routed to its decline fallback names both models:
        the chain error otherwise reads as if only the fallback was asked.
        """
        served = self._target_model(self.primary, routed or model)
        requested = self._target_model(self.primary, model) if routed else None
        return (self._config_of(self.primary), served, requested)

    @staticmethod
    def _config_of(provider: BaseLLMProvider) -> str | None:
        """The admin's label of a target's config, else its id, for a failover leg."""
        label = getattr(provider, "provider_config_label", None)
        if isinstance(label, str) and label:
            return label
        config_id = getattr(provider, "provider_config_id", None)
        return str(config_id) if config_id else None

    def _chain_exhausted(
        self,
        failures: list[tuple[str, Exception]],
        tried: list[tuple[str | None, ...]] | None = None,
    ) -> AllProvidersFailedError:
        """The error of a failover chain with no target left.

        When no target was tried, every candidate was left out as unable to
        chat: the error names them, or it would read as if no failover were
        configured at all.
        """
        skipped: list[tuple[str, str]] = []
        if len(failures) == 1:
            skipped = [
                (provider.provider_name, self._target_model(provider, model) or "<provider default>")
                for provider, model in self._fallback_candidates()
                if not self._can_chat(provider, model)
            ]
        return AllProvidersFailedError(failures, skipped=skipped, tried=tried)

    @staticmethod
    def _capped_out(provider: BaseLLMProvider, failures: list[tuple[str, Exception]]) -> bool:
        """Its own body cap already refused the request, as it will on every leg."""
        return any(name == provider.provider_name and _is_own_body_cap(exc, provider) for name, exc in failures)

    @staticmethod
    def _is_non_failoverable(exc: Exception, provider: BaseLLMProvider | None = None) -> bool:
        """Return True if the error is caused by the request itself (not the provider).

        These errors will reproduce on every provider, so failing over is
        pointless and wastes time (each attempt can block for the full
        timeout before returning the same error). A model's decline is
        handed to another model only in the categories
        ``ModelDeclinedError.failover_allowed`` names.
        """
        if _is_own_body_cap(exc, provider):
            return False
        if isinstance(exc, ContextOverflowError):
            return True
        if isinstance(exc, ModelDeclinedError):
            return not exc.failover_allowed

        # HTTP 413 / payload-size errors — the conversation / payload is too
        # big for the provider's API gateway.  Retrying the same payload on a
        # different provider will produce the same result, so fail fast with
        # the size error instead of burning all fallback slots.
        if is_request_too_large_error(exc):
            return True
        msg = str(exc).lower()
        return any(
            kw in msg
            for kw in (
                "prompt is too long",
                "too many tokens",
                "maximum context length",
                "context_length_exceeded",
                # Request-content 400s reproduce on every provider — fail
                # fast. The PDF page cap ("A maximum of 600 PDF
                # pages may be provided") is a REQUEST_TOO_LARGE_KEYWORDS entry
                # and is caught by ``is_request_too_large_error`` above.
                "at least one message is required",
                "messages: at least one",
                "empty messages",
            )
        )

    @staticmethod
    def _resolve_max_tokens_limit(provider: BaseLLMProvider, model: str | None) -> int | None:
        """Resolve max output tokens for *provider* and optional *model*.

        Uses the same priority chain as the agentic loop (admin override ->
        metadata snapshot -> model-family floor -> provider flat default) so the
        failover clamp never caps a freshly released model to a stale value
        (e.g. clamping ``claude-opus-4-7`` to 8_192).
        """
        # No final fallback: the chain still returns an int from an admin
        # override / snapshot / family floor even when the provider declares no
        # flat limit (clamping to that resolved value is intended). It returns
        # ``None`` ONLY when nothing resolves, so a provider with no configured
        # limit yields "no clamp" rather than a forced fallback value (callers
        # guard on ``isinstance(limit, int)``).
        return resolve_provider_max_output_tokens(provider, model)

    # Sentinel for ``max_tokens=`` callers who want the helper to fall
    # back to whatever ``kwargs`` already carries (or to no key at all
    # when neither is provided).  The string sentinel is distinct from
    # both ``None`` (a valid explicit "no cap" value the caller may
    # legitimately want to forward to the fallback) and ``int`` (an
    # explicit cap).
    _MAX_TOKENS_UNSET: ClassVar[str] = "__UNSET__"

    @classmethod
    def _fallback_kwargs_for_provider(
        cls,
        provider: BaseLLMProvider,
        *,
        model: str | None,
        kwargs: dict,
        max_tokens: int | None | str = _MAX_TOKENS_UNSET,
        same_provider: bool = False,
    ) -> dict:
        """Build sanitized kwargs for a fallback provider call.

        Strips kwargs in ``_FALLBACK_EXCLUDED_KWARGS`` (``structured_output``
        is formatted for the primary provider and incompatible with the
        fallback; ``reasoning_effort`` is provider/model-specific and can
        400 on fallback) and resolves the final ``max_tokens`` value. With
        *same_provider* (the target is a decline fallback - ``index < declined``
        in the loop - so the call runs on ``self.primary`` with another of its
        own models) both are kept: that provider fits them to each model. Every
        other target, an admin fallback model on the primary's own config
        included (it is a chain target, not a decline fallback, whether or not
        the provider cache happens to hand back the primary's instance), still
        gets them stripped.

        Three call shapes are supported:

        * ``max_tokens=<int>`` — overrides any value already in ``kwargs``
          and is clamped to the fallback provider/model limit.  The
          key is **always** present in the returned dict.
        * ``max_tokens=None`` — the caller is explicitly asking the
          fallback to run with no token cap (e.g. when the upstream
          ``chat_stream`` signature lets a caller pass
          ``max_tokens=None`` for "use the provider default").  The
          key is **always** present in the returned dict (set to
          ``None``) so callers can pop unconditionally.
        * ``max_tokens=<sentinel>`` (default) — fall back to whatever
          ``kwargs`` carries.  If ``kwargs`` doesn't have ``max_tokens``
          either, the returned dict won't have the key — callers that
          unconditionally pop must use the default-aware
          ``pop("max_tokens", None)`` form in that case.
        """
        fallback_kwargs = (
            dict(kwargs)
            if same_provider
            else {k: v for k, v in kwargs.items() if k not in _FALLBACK_EXCLUDED_KWARGS}
        )
        if max_tokens is not cls._MAX_TOKENS_UNSET:
            # Always write the key — even when ``None`` — so the caller's
            # ``pop("max_tokens")`` cannot ``KeyError``.  This is the
            # contract the streaming/chat failover paths rely on.
            fallback_kwargs["max_tokens"] = max_tokens
        requested_max_tokens = fallback_kwargs.get("max_tokens")
        if isinstance(requested_max_tokens, int) and requested_max_tokens > 0:
            limit = cls._resolve_max_tokens_limit(provider, model)
            if isinstance(limit, int) and requested_max_tokens > limit:
                logger.info(
                    "Clamping failover max_tokens for provider '%s' from %s to %s (model=%s)",
                    provider.provider_name,
                    requested_max_tokens,
                    limit,
                    model or getattr(provider, "default_model", None),
                )
                fallback_kwargs["max_tokens"] = limit
        return fallback_kwargs

    async def _fit_tools(self, kwargs: dict, provider: BaseLLMProvider, model: str | None) -> dict:
        """*kwargs* with its tool list cut to what *model* on *provider* accepts."""
        tools = kwargs.get("tools")
        if not tools:
            return kwargs
        limit = await provider_max_tools(provider, model)
        truncated = self._truncate_tools(tools, limit)
        if truncated is tools:
            return kwargs
        logger.warning(
            "Truncating tool list from %d to %d for provider '%s', model %s "
            "(max_tools=%d) — dropped %d lower-priority tool(s).",
            len(tools),
            len(truncated),
            getattr(provider, "provider_name", "unknown"),
            model,
            limit,
            len(tools) - len(truncated),
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Dropped tools during failover truncation: %s",
                [getattr(t, "name", "?") for t in tools if t not in truncated],
            )
        return {**kwargs, "tools": truncated}

    @staticmethod
    def _truncate_tools(tools: list, limit: int) -> list:
        """*tools* cut to *limit*, the lowest ``tool_retention_key`` kept; *tools* itself when it fits.

        The key ranks the source category (``builtin`` < ``custom`` < ``mcp``
        < ``integration``) before the per-tool ``priority``, so integrations
        can never evict the platform's builtins. The agent loop's ungated
        truncation and the progressive-disclosure gate use the same key, so
        the primary and a fallback keep the same tools.
        """
        if len(tools) <= limit:
            return tools
        return sorted(tools, key=tool_retention_key)[:limit]

    @staticmethod
    def _with_on_token(kwargs: dict, watched: _WatchedTokens | None) -> dict:
        return {**kwargs, "on_token": watched} if watched is not None else kwargs

    def _primary_served(self) -> None:
        record_primary_served(self, self.primary)

    def _fallback_served(
        self,
        fallback: BaseLLMProvider,
        served_model: str | None = None,
        usage_sink: StreamUsageSink | None = None,
    ) -> None:
        record_failover_served(fallback, owner=self)
        # A text stream carries no model of its own, and the model a
        # response names cannot tell a fallback from the primary: a
        # non-streamed OpenAI or Anthropic call names the API's dated model,
        # a streamed one the model it was asked for.
        self.last_model = served_model
        if usage_sink is not None:
            # An aborted stream's generation is closed from its usage sink:
            # name the fallback's model there too.
            usage_sink.note_served_model(served_model)

    @staticmethod
    async def _restart_stream(
        on_stream_reset: Callable[[], Awaitable[None]] | None,
        *,
        has_next: bool,
        source: str,
        exc: Exception,
    ) -> bool:
        """Whether a call whose output already reached the caller may move on.

        Only when another target remains and the caller took back what it
        received (``on_stream_reset``): the caller is reset first, so the
        next answer streams from the start instead of after the partial one.
        Otherwise the error surfaces and the partial output stays as it is. A
        reset that raises did not take the output back, so it refuses the
        failover too: fail closed, never a second answer after the first.
        """
        if on_stream_reset is None or not has_next:
            logger.warning(
                "LLM provider '%s' failed AFTER streaming partial output: %s - "
                "not failing over (%s)",
                source,
                format_exception_message(exc),
                "no other provider to try" if on_stream_reset is not None
                else "the caller cannot take the output back",
            )
            return False
        try:
            await on_stream_reset()
        except Exception as reset_exc:
            logger.warning(
                "LLM provider '%s' failed AFTER streaming partial output: %s - "
                "not failing over (the caller could not take the output back: %s)",
                source,
                format_exception_message(exc),
                format_exception_message(reset_exc),
            )
            return False
        logger.warning(
            "LLM provider '%s' failed after streaming partial output: %s - "
            "took the output back, failing over",
            source,
            format_exception_message(exc),
        )
        return True

    async def _failover_call(
        self, method_name: str, messages, *, model=None, **kwargs,
    ):
        """Execute *method_name* on primary, falling back on failure."""
        failures: list[tuple[str, Exception]] = []
        # ``(config, model)`` of each failed target, in step with *failures*.
        tried: list[tuple[str | None, ...]] = []
        on_stream_reset = kwargs.pop("on_stream_reset", None)
        on_token = kwargs.get("on_token")
        targets: list[tuple[BaseLLMProvider, str | None]] = []
        declined = 0
        self.last_model = None
        routed = self._routed_model(model)
        served = self._primary_served_as(routed, usage_sink_of(kwargs))
        watched = _WatchedTokens(on_token, served) if on_token is not None else None
        try:
            # A decline route may lead to a model with a smaller tool cap.
            primary_kwargs = await self._fit_tools(kwargs, self.primary, routed) if routed else kwargs
            result = await getattr(self.primary, method_name)(
                messages, model=routed or model, **self._with_on_token(primary_kwargs, watched),
            )
        except Exception as exc:
            failures.append((self.primary.provider_name, exc))
            tried.append(self._primary_leg(model, routed))
            if self._is_non_failoverable(exc, self.primary):
                logger.warning(
                    "Primary LLM provider '%s' %s failed with non-failoverable error (model=%s): %s",
                    self.primary.provider_name, method_name, routed or model, format_exception_message(exc),
                )
                raise
            targets, declined = self._targets_after(exc, model, routed=routed)
            if (
                watched is not None
                and watched.emitted
                and not await self._restart_stream(
                    on_stream_reset,
                    has_next=bool(targets),
                    source=self.primary.provider_name,
                    exc=exc,
                )
            ):
                raise
            logger.warning(
                "Primary LLM provider '%s' %s failed (model=%s): %s. Trying fallbacks...",
                self.primary.provider_name, method_name, routed or model, format_exception_message(exc),
            )
        else:
            served()
            return result

        for index, (fallback, fallback_model) in enumerate(targets):
            served_model = self._target_model(fallback, fallback_model)
            target = self._describe_target(fallback, served_model)
            watched = (
                _WatchedTokens(
                    on_token,
                    partial(self._fallback_served, fallback, served_model, usage_sink_of(kwargs)),
                )
                if on_token is not None
                else None
            )
            if self._capped_out(fallback, failures):
                logger.info("Skipping '%s' (%s): its body cap refused this request", fallback.provider_name, target)
                continue
            if index >= declined and failures and failures[-1][0] == fallback.provider_name:
                # A decline hand-off is a different answer, not a retry; a
                # second leg on the provider that just failed is a retry.
                await _same_provider_backoff()
            try:
                logger.info(
                    "Failing over %s to '%s' (%s)",
                    method_name,
                    fallback.provider_name,
                    target,
                )
                # Build the per-fallback kwargs in two passes that compose
                # the failover-time sanitization rules added by recent
                # PRs against this codepath:
                #   1. ``_fallback_kwargs_for_provider`` strips
                #      ``structured_output`` / ``reasoning_effort`` and
                #      clamps ``max_tokens`` to the fallback provider's
                #      per-model / per-provider output limit.
                #   2. ``_fit_tools`` trims the tool list to the
                #      fallback model's cap (``max_tools_for``)
                #      — Anthropic allows 1024 while OpenAI caps at 128,
                #      so a primary→fallback failover with a large tool
                #      list would otherwise hit ``array_above_max_length``.
                per_fallback_kwargs = self._fallback_kwargs_for_provider(
                    fallback,
                    model=fallback_model,
                    kwargs=kwargs,
                    max_tokens=kwargs.get("max_tokens"),
                    same_provider=index < declined,
                )
                per_fallback_kwargs = await self._fit_tools(per_fallback_kwargs, fallback, served_model)
                result = await getattr(fallback, method_name)(
                    messages,
                    model=fallback_model,
                    **self._with_on_token(per_fallback_kwargs, watched),
                )
                self._fallback_served(fallback, served_model)
                if index < declined:
                    self._route_declined(model, fallback_model)
                return result
            except Exception as fb_exc:
                failures.append((fallback.provider_name, fb_exc))
                tried.append((self._config_of(fallback), served_model))
                self._record_fallback_failure(fallback, fallback_model, fb_exc)
                if self._is_non_failoverable(fb_exc, fallback):
                    # A request-caused error surfacing mid-chain (e.g. the
                    # primary failed transiently, then a fallback got the 413)
                    # reproduces on every remaining target too — stop here
                    # instead of burning the rest of the chain.
                    logger.warning(
                        "Fallback LLM provider '%s' %s failed with "
                        "non-failoverable error (%s): %s — not trying further fallbacks",
                        fallback.provider_name, method_name, target,
                        format_exception_message(fb_exc),
                    )
                    raise AllProvidersFailedError(failures, tried=tried) from fb_exc
                if (
                    watched is not None
                    and watched.emitted
                    and not await self._restart_stream(
                        on_stream_reset,
                        has_next=index + 1 < len(targets),
                        source=fallback.provider_name,
                        exc=fb_exc,
                    )
                ):
                    raise AllProvidersFailedError(failures, tried=tried) from fb_exc
                logger.warning(
                    "Fallback LLM provider '%s' %s also failed (%s): %s",
                    fallback.provider_name, method_name, target,
                    format_exception_message(fb_exc),
                )

        raise self._chain_exhausted(failures, tried)

    async def chat(
        self,
        messages,
        model=None,
        temperature=0.7,
        max_tokens=4096,
        tools=None,
        **kwargs,
    ):
        return await self._failover_call(
            "chat", messages, model=model,
            temperature=temperature, max_tokens=max_tokens, tools=tools,
            **kwargs,
        )

    async def chat_stream(
        self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs
    ):
        """Stream with failover — try primary, then fallbacks."""
        failures: list[tuple[str, Exception]] = []
        tried: list[tuple[str | None, ...]] = []
        on_stream_reset = kwargs.pop("on_stream_reset", None)
        targets: list[tuple[BaseLLMProvider, str | None]] = []
        # Reset so a stale value from a prior call can't leak through if the
        # underlying provider doesn't set it on this run.
        self.last_stop_reason = None
        self.last_usage = None
        self.last_trace_metadata = None
        self.last_provider_extra = None
        self.last_model = None
        primary_yielded = False
        declined = 0
        routed = self._routed_model(model)
        served = self._primary_served_as(routed, usage_sink_of(kwargs))
        try:
            async for chunk in self.primary.chat_stream(
                messages,
                model=routed or model,
                temperature=temperature,
                max_tokens=max_tokens,
                **(await self._fit_tools(kwargs, self.primary, routed) if routed else kwargs),
            ):
                if not primary_yielded:
                    primary_yielded = True
                    served()
                yield chunk
            # Surface the provider's streaming metadata on the wrapper so
            # callers reading ``llm.last_usage`` / ``llm.last_stop_reason``
            # (e.g. the agentic loop) see what actually streamed — without
            # this the Failover wrapper would always report zero usage.
            self.last_stop_reason = getattr(self.primary, "last_stop_reason", None)
            self.last_usage = getattr(self.primary, "last_usage", None)
            self.last_trace_metadata = getattr(self.primary, "last_trace_metadata", None)
            self.last_provider_extra = getattr(self.primary, "last_provider_extra", None)
            if not primary_yielded:  # an empty answer is served too
                served()
            return
        except Exception as exc:
            failures.append((self.primary.provider_name, exc))
            tried.append(self._primary_leg(model, routed))
            if self._is_non_failoverable(exc, self.primary):
                logger.warning(
                    "Primary LLM stream failed with non-failoverable error (model=%s): %s",
                    routed or model,
                    format_exception_message(exc),
                )
                raise
            targets, declined = self._targets_after(exc, model, routed=routed)
            # Partial output already reached the caller: restarting the
            # stream on a fallback is only safe once the caller took it back,
            # never by concatenating two responses.
            if primary_yielded and not await self._restart_stream(
                on_stream_reset,
                has_next=bool(targets),
                source=self.primary.provider_name,
                exc=exc,
            ):
                raise
            logger.warning(
                "Primary LLM provider '%s' stream failed (model=%s): %s. Trying fallbacks...",
                self.primary.provider_name,
                routed or model,
                format_exception_message(exc),
            )
        usage_sink = usage_sink_of(kwargs)
        for index, (fallback, fallback_model) in enumerate(targets):
            fallback_yielded = False
            served_model = self._target_model(fallback, fallback_model)
            target = self._describe_target(fallback, served_model)
            if self._capped_out(fallback, failures):
                logger.info("Skipping '%s' (%s): its body cap refused this request", fallback.provider_name, target)
                continue
            if index >= declined and failures and failures[-1][0] == fallback.provider_name:
                await _same_provider_backoff()
            try:
                logger.info(
                    "Streaming failover to '%s' (%s)",
                    fallback.provider_name,
                    target,
                )
                # Same sanitization composition as ``_failover_call``:
                # strip incompatible kwargs + clamp ``max_tokens`` first,
                # then truncate the tool list to the fallback's cap.
                fallback_kwargs = self._fallback_kwargs_for_provider(
                    fallback,
                    model=fallback_model,
                    kwargs=kwargs,
                    max_tokens=max_tokens,
                    same_provider=index < declined,
                )
                fallback_kwargs = await self._fit_tools(fallback_kwargs, fallback, served_model)
                # ``_fallback_kwargs_for_provider`` is guaranteed to set
                # ``max_tokens`` in the returned dict whenever any value was
                # provided (here it always is, since ``max_tokens`` has a
                # signature default).  Pop unconditionally so the post-clamp
                # value reaches ``chat_stream`` exactly once.
                clamped_max_tokens = fallback_kwargs.pop("max_tokens")
                async for chunk in fallback.chat_stream(
                    messages,
                    model=fallback_model,
                    temperature=temperature,
                    max_tokens=clamped_max_tokens,
                    **fallback_kwargs,
                ):
                    if not fallback_yielded:
                        fallback_yielded = True
                        # Serving from its first chunk on, even if the stream
                        # then errors or is cancelled, under the model
                        # the fallback was called with.
                        self._fallback_served(fallback, served_model, usage_sink)
                    yield chunk
                self.last_stop_reason = getattr(fallback, "last_stop_reason", None)
                self.last_usage = getattr(fallback, "last_usage", None)
                self.last_trace_metadata = getattr(fallback, "last_trace_metadata", None)
                self.last_provider_extra = getattr(fallback, "last_provider_extra", None)
                if not fallback_yielded:  # an empty answer is served too
                    self._fallback_served(fallback, served_model, usage_sink)
                if index < declined:
                    self._route_declined(model, fallback_model)
                return
            except Exception as fb_exc:
                failures.append((fallback.provider_name, fb_exc))
                tried.append((self._config_of(fallback), served_model))
                self._record_fallback_failure(fallback, fallback_model, fb_exc)
                if self._is_non_failoverable(fb_exc, fallback):
                    # Same rationale as ``_failover_call``: a request-caused
                    # error (413 payload size, context overflow) reproduces on
                    # every remaining target — stop the chain here.
                    logger.warning(
                        "Fallback LLM provider '%s' stream failed with "
                        "non-failoverable error (%s): %s — not trying further fallbacks",
                        fallback.provider_name,
                        target,
                        format_exception_message(fb_exc),
                    )
                    raise AllProvidersFailedError(failures, tried=tried) from fb_exc
                # Same guard as the primary's.
                if fallback_yielded and not await self._restart_stream(
                    on_stream_reset,
                    has_next=index + 1 < len(targets),
                    source=fallback.provider_name,
                    exc=fb_exc,
                ):
                    raise AllProvidersFailedError(failures, tried=tried) from fb_exc
                logger.warning(
                    "Fallback LLM provider '%s' stream also failed (%s): %s",
                    fallback.provider_name,
                    target,
                    format_exception_message(fb_exc),
                )

        raise self._chain_exhausted(failures, tried)

    async def chat_stream_with_tools(
        self, messages, tools=None, *, on_token=None, model=None,
        temperature=0.7, max_tokens=4096, **kwargs,
    ):
        """Stream with tools + failover."""
        return await self._failover_call(
            "chat_stream_with_tools", messages, model=model,
            tools=tools, on_token=on_token,
            temperature=temperature, max_tokens=max_tokens, **kwargs,
        )

    async def embed(self, text, model=None):
        """Embedding does not failover — delegates to primary."""
        return await self.primary.embed(text, model=model)

    async def list_models(self):
        """List models from all providers."""
        models = await self.primary.list_models()
        for fb in self.fallbacks:
            with contextlib.suppress(Exception):
                models.extend(await fb.list_models())
        return models


async def get_platform_default_model(db: AsyncSession) -> str | None:
    """Return the platform default model.

    Returns ``PlatformSettings.agent_default_model`` first (the dedicated agent
    default), falling back to ``GeneralAssistantConfig.llm_model`` (the GA's own
    model) for backwards compatibility.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings

    ps = await get_platform_settings(db)
    if ps.agent_default_model:
        return ps.agent_default_model

    from app.models.general_assistant_config import GeneralAssistantConfig

    result = await db.execute(select(GeneralAssistantConfig).limit(1))
    ga = result.scalar_one_or_none()
    return ga.llm_model if ga else None


async def resolve_provider_for_model(
    db: AsyncSession,
    model_id: str,
    *,
    user_id: object = None,
) -> tuple[BaseLLMProvider, str]:
    """Look up which provider owns *model_id* via ``available_models``.

    Falls back to prefix-based inference if the model is not found in any
    provider's ``available_models`` list. Only the providers *user_id* may use
    are considered, theirs first; with no user, the unrestricted ones.

    Returns:
        ``(provider_instance, model_id)``

    Raises:
        ProviderNotUsableError: when providers exist but none the caller may
            use serves the model (no usable provider, or a model only other
            teams' providers list).
        ValueError: if no matching provider is found.
    """
    result = await db.execute(select_provider_configs())
    routing = await provider_routing(db, user_id, result.scalars().all())
    if routing.audience.ai_disabled or (routing.configs and not routing.usable):
        raise ProviderNotUsableError(routing.no_usable_message())
    owner = routing.owner_for(model_id)
    if owner is not None:
        return await _cached_provider(owner), model_id
    if routing.is_reserved(model_id):
        raise ProviderNotUsableError(routing.reserved_model_message(model_id))

    raise ValueError(
        f"Model '{model_id}' is not available. Check the available models in Settings > AI and models > Models."
    )


async def get_available_models(
    db: AsyncSession,
    *,
    user_id: object = None,
    every_provider: bool = False,
    routing: ProviderRouting | None = None,
) -> list[dict[str, str | bool]]:
    """Return the models of the providers *user_id* may use.

    With no user, the unrestricted providers only. *every_provider* lists every
    configured provider instead, for an admin configuring the platform. A
    caller that already holds the *routing* of *user_id* passes it, and no
    provider is read again.

    Each item is ``{"model": "<model_id>", "provider": "<provider_name>",
    "provider_id": "<uuid>", "access_restricted": <bool>}``.
    """
    if routing is not None and not every_provider:
        configs = list(routing.usable)
    else:
        result = await db.execute(select_provider_configs())
        configs = list(result.scalars().all())
        if not every_provider:
            configs = (await provider_routing(db, user_id, configs)).usable
    models: list[dict[str, str | bool]] = []
    for cfg in configs:
        # ``available_models`` is an unconstrained JSON list: the update endpoint
        # stores the request verbatim and the env merge keeps whatever the stored
        # catalog already held, so one config CAN list a model twice. Deduplicate
        # per config (order-preserving) so ``(provider_id, model)`` identifies an
        # offering uniquely — the pickers use that pair as their option value and
        # React key, and a repeat would put back the duplicate-key and
        # doubled-trigger symptoms.
        for m in dict.fromkeys(cfg.available_models or []):
            models.append(
                {
                    "model": m,
                    "provider": cfg.provider,
                    "provider_id": str(cfg.id),
                    "access_restricted": is_restricted(cfg),
                }
            )
    return models


# How many available-model ids to embed in a validation error message.
# Enough for the caller (human or LLM agent) to self-correct without
# turning the error into a wall of text on installs with many providers.
_MODEL_SUGGESTION_LIMIT = 15


def _model_sample_sentence(models: set[str]) -> str:
    if not models:
        return ""
    sample = ", ".join(sorted(models)[:_MODEL_SUGGESTION_LIMIT])
    if len(models) > _MODEL_SUGGESTION_LIMIT:
        sample += ", ..."
    return f" Available models: {sample}"


async def validate_selectable_chat_model(
    db: AsyncSession,
    model_id: str,
    *,
    user_id: object = None,
    scoped: bool = False,
) -> str | None:
    """Validate that *model_id* is in a configured provider's curated catalog.

    *scoped* checks it against the providers *user_id* may use instead of the
    whole catalog: a non-admin saving a model on an agent may only pick
    one of theirs. Admins and the platform-wide settings keep the whole catalog.

    Returns ``None`` when the model is selectable, or a human/LLM-friendly
    error message (including a sample of valid model ids) when it is not.

    This is the write-path guard for every surface that persists a chat
    model reference (agent create/update via REST, the agent-builder tool,
    GA / platform default updates): the **runtime** resolver deliberately
    falls back to prefix inference (``_resolve_model_owner_config``) so
    newly released models keep working before an admin refreshes the
    catalog — but that same leniency means a stale or hallucinated model
    id (e.g. a retired, date-suffixed Anthropic snapshot) routes to the
    provider and 404s on every call. Persisting only catalog
    members keeps the curated ``available_models`` lists (env-seeded under
    ``MODELS_ENV_PROVIDING_ONLY``, otherwise admin-managed) the single
    source of truth for what can be *selected*, without touching the
    runtime leniency.

    For a **chat-capable** model, validation is intentionally skipped
    (returns ``None``) when no provider exposes any ``available_models``
    yet — on a fresh install or a bootstrap import there is no catalog to
    validate against, and the runtime already fails with an explicit "No LLM
    provider configured" error in that state.

    A non-chat model (image / embedding / audio / moderation / rerank) is
    rejected **first and unconditionally** (before the empty-catalog skip, and
    even when it IS a catalog member): such models back the Image Generation /
    Embeddings sections, not chat, and persisting one as an agent / GA /
    platform default chat model is the root cause of the "my default model is
    an image model" failure.
    """
    if not is_chat_capable_model(model_id):
        return (
            f"Model '{model_id}' is not a chat-capable model (image, embedding, "
            "audio or similar) and cannot be selected as a chat model. Pick a "
            "chat-capable model instead."
        )
    result = await db.execute(select(LLMProviderConfig))
    configs = list(result.scalars().all())
    routing = await provider_routing(db, user_id, configs) if scoped else None
    available: set[str] = set()
    for cfg in configs:
        available.update(m for m in (cfg.available_models or []) if m)
    if not available:
        return None
    if routing is not None:
        usable = usable_model_ids(routing)
        if model_id in usable:
            return None
        if model_id in available:
            return (
                f"Model '{model_id}' is only offered by LLM providers you do "
                "not have access to, so you cannot select it."
                + _model_sample_sentence(usable)
            )
        available = usable
    elif model_id in available:
        return None
    if not available:
        return (
            f"Model '{model_id}' cannot be selected: none of the LLM providers "
            "you have access to offers a model."
        )
    sample = ", ".join(sorted(available)[:_MODEL_SUGGESTION_LIMIT])
    if len(available) > _MODEL_SUGGESTION_LIMIT:
        sample += ", ..."
    return (
        f"Model '{model_id}' is not in any configured provider's available "
        f"models, so it cannot be selected. Available models: {sample}"
    )


async def _get_platform_settings(db: AsyncSession):
    """Load the PlatformSettings singleton (always returns an instance)."""
    from app.api.v1.platform_settings_helpers import get_platform_settings

    return await get_platform_settings(db)


async def configured_video_models(db: AsyncSession) -> tuple[str | None, list[str]]:
    """``(default, fallbacks)``: the configured default video model (``PlatformSettings.video_model``, ``None`` when
    unset) and the video models an AI shot may fall back to, in order, once each
    (``PlatformSettings.video_fallback_models``), both from Settings > AI and models > Media > Video generation.

    The fallbacks are empty by default: no shot is then made on any model but
    the default one (:func:`get_video_provider` with no model). The text
    models' fallback (``fallback_model``) is a separate setting.
    """
    ps = await _get_platform_settings(db)
    default = (getattr(ps, "video_model", None) or "").strip() or None
    raw = getattr(ps, "video_fallback_models", None) or []
    names = dict.fromkeys(item.strip() for item in raw if isinstance(item, str) and item.strip())
    return default, [name for name in names if name != default]


# Default models per provider when no admin preference is stored.
# Also used by api/v1/settings.py for auto-setting defaults on first provider config.
EMBEDDING_DEFAULTS: dict[str, str] = {
    "openai": "text-embedding-3-large",
    "gemini": "text-embedding-004",
    "mistral": "mistral-embed",
    "ollama": "nomic-embed-text",
    "azure_openai": "text-embedding-3-large",
    "bedrock": "amazon.titan-embed-text-v2:0",
}
IMAGE_DEFAULTS: dict[str, str] = {
    "openai": "gpt-image-1",
    "gemini": "imagen-3.0-generate-002",
}
# Provider preference order for each capability
_EMBEDDING_PROVIDERS = (
    "openai",
    "gemini",
    "azure_openai",
    "mistral",
    "bedrock",
    "ollama",
)
_IMAGE_PROVIDERS = ("openai", "gemini", "azure_openai")
# Embedding-only providers (TEI) need no explicit embedding model — their
# native ``/embed`` route ignores the model id. Same membership as
# :data:`EMBEDDING_ONLY_PROVIDERS` by construction.
_EMBEDDING_MODEL_OPTIONAL_PROVIDERS = EMBEDDING_ONLY_PROVIDERS


async def gateway_default(
    configs: Iterable[LLMProviderConfig], kind: str
) -> tuple[LLMProviderConfig, BaseLLMProvider, str] | None:
    """The first AI Gateway of *configs* marking a default *kind* model, the last fallback (#746).

    Read from its listing at each resolution, never written to ``PlatformSettings``.
    """
    for config in configs:
        if config.provider != FiligranGatewayProvider.provider_name:
            continue
        provider = await _cached_provider(config)
        if model := await provider.marked_default_model(kind):
            return config, provider, model
    return None


def implicit_embedding_fallback(
    configs: list[LLMProviderConfig],
    *,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> tuple[LLMProviderConfig, str] | None:
    """The config and model embeddings use when no admin choice resolves.

    The first provider of :data:`_EMBEDDING_PROVIDERS` that has a config - the
    newest config of that provider, *configs* being in
    :func:`select_provider_configs` order - with that provider's built-in
    default model. ``None`` when no embedding-capable provider is configured.

    Shared by :func:`get_embedding_provider` and the provider-deletion
    preview, which tells an admin where embeddings go once the embedding
    default is cleared.
    """
    configs_by_provider: dict[str, list[LLMProviderConfig]] = {}
    for cfg in configs:
        configs_by_provider.setdefault(cfg.provider, []).append(cfg)
    for prov_name in _EMBEDDING_PROVIDERS:
        matching = configs_by_provider.get(prov_name, [])
        if matching:
            model = (
                embedding_model if embedding_provider == prov_name else None
            ) or EMBEDDING_DEFAULTS[prov_name]
            return newest_config(matching), model
    return None


async def get_embedding_provider(
    db: AsyncSession,
    *,
    user_id: object = None,
) -> tuple[BaseLLMProvider, str, uuid.UUID | None]:
    """Get the platform embedding provider, model, and provider config UUID.

    Returns a 3-tuple ``(provider_instance, model_string, provider_config_id)``.
    The ``provider_config_id`` is the UUID primary key of the ``LLMProviderConfig``
    row that was used, or ``None`` when falling back to a provider without an
    explicit admin-configured embedding assignment.

    Resolution:
    1. Admin-configured ``embedding_provider`` / ``embedding_model`` from
       ``PlatformSettings``.
    2. Fallback: :func:`implicit_embedding_fallback`, the first configured
       provider in :data:`_EMBEDDING_PROVIDERS` order, with its built-in
       default model.
    3. With no embedding model stored, an AI Gateway's flagged embedding
       model, saved as the default where it re-embeds no stored vector
       (:mod:`app.llm.gateway_embedding_default`).

    Uses a single DB query for all provider configs and the provider
    instance cache to avoid redundant Fernet decrypt + SDK client creation.

    Platform-level for everyone, restricted providers included, but
    never for a person the resolved provider excludes: *user_id*, else
    the person the context acts for. Their content is refused with
    :class:`ProviderNotUsableError` rather than embedded by it.
    """
    ps = await _get_platform_settings(db)

    from app.models.embedding import EMBEDDING_DIMENSIONS

    configured_dimensions = ps.embedding_dimensions or EMBEDDING_DIMENSIONS
    # The explicitly-configured dimension (``None`` unless an admin set one).
    # Unlike ``configured_dimensions`` (which falls back to the platform
    # default for the custom_openai validation path), this is forwarded to every
    # provider but Custom OpenAI verbatim so that an *unset* dimension keeps the model's
    # native size — passing the 3072 default to ``text-embedding-3-small``
    # (native 1536) would otherwise fail every embed call.
    explicit_dimensions = ps.embedding_dimensions

    def _with_embedding_dimensions(provider: BaseLLMProvider) -> BaseLLMProvider:
        provider.embedding_dimensions = (
            configured_dimensions if isinstance(provider, CustomOpenAIProvider) else explicit_dimensions
        )
        return provider

    result = await db.execute(select_provider_configs())
    configs = list(result.scalars().all())
    configs_by_provider: dict[str, list[LLMProviderConfig]] = {}
    for cfg in configs:
        configs_by_provider.setdefault(cfg.provider, []).append(cfg)

    def _admin_configured() -> tuple[LLMProviderConfig, str] | None:
        if getattr(ps, "embedding_provider_id", None):
            config = next((c for c in configs if c.id == ps.embedding_provider_id), None)
            if config:
                model = ps.embedding_model or config.default_model or ""
                if model or config.provider in _EMBEDDING_MODEL_OPTIONAL_PROVIDERS:
                    return config, model
        if ps.embedding_provider:
            matching = configs_by_provider.get(ps.embedding_provider, [])
            if matching:
                config = newest_config(matching)
                model = ps.embedding_model or config.default_model or ""
                if model or config.provider in _EMBEDDING_MODEL_OPTIONAL_PROVIDERS:
                    return config, model
        return None

    # 1. Admin-configured (UUID first)
    if (chosen := _admin_configured()) is not None:
        config, model = chosen
        await ensure_not_excluded(db, config, "Embedding", user_id)
        return _with_embedding_dimensions(await _cached_provider(config)), model, config.id

    # 2. Fallback: first embedding-capable provider
    fallback = implicit_embedding_fallback(
        configs,
        embedding_provider=ps.embedding_provider,
        embedding_model=ps.embedding_model,
    )
    if fallback is not None:
        config, model = fallback
        await ensure_not_excluded(db, config, "Embedding", user_id)
        return _with_embedding_dimensions(await _cached_provider(config)), model, config.id

    # 3. No other provider embeds: the AI Gateway's flagged model, saved first.
    if not ps.embedding_model and any(c.provider == FiligranGatewayProvider.provider_name for c in configs):
        from app.llm.gateway_embedding_default import fill_at_lookup

        # A choice saved meanwhile is copied onto *ps*: served as step 1 would.
        if (adopted := await fill_at_lookup(ps, configs) or _admin_configured()) is not None:
            config = next((c for c in configs if c.id == adopted[0].id), adopted[0])
            await ensure_not_excluded(db, config, "Embedding", user_id)
            return _with_embedding_dimensions(await _cached_provider(config)), adopted[1], config.id

    raise EmbeddingConfigurationError(
        "An embedding-capable provider is required (OpenAI, Gemini, Azure OpenAI, Mistral, Bedrock, Ollama, "
        "a Custom OpenAI-compatible endpoint, or a /embed-compatible embedding server). "
        "An admin must configure a provider in Settings. For a Custom OpenAI-compatible endpoint, "
        "they must explicitly set embedding_provider=custom_openai and embedding_model, because "
        "there is no default embedding model for custom_openai."
    )


async def get_image_provider(
    db: AsyncSession,
    *,
    user_id: object = None,
) -> tuple[BaseLLMProvider, str]:
    """Get the platform image generation provider and model.

    Resolution:
    1. Admin-configured ``image_provider`` / ``image_model`` from
       ``PlatformSettings``.
    2. Fallback: the first configured provider in :data:`_IMAGE_PROVIDERS`
       order that has a model (its built-in default, or the admin's
       ``image_model`` for that provider; Azure OpenAI has no default).
    3. Last: the default image model of the gateway the admin chose, else of
       the newest AI Gateway (:func:`gateway_default`).

    Uses a single DB query for all provider configs and the provider
    instance cache.

    Never for a person the resolved provider excludes: *user_id*, else
    the person the context acts for (:class:`ProviderNotUsableError`).
    """
    ps = await _get_platform_settings(db)

    result = await db.execute(select_provider_configs())
    configs = list(result.scalars().all())
    configs_by_provider: dict[str, list[LLMProviderConfig]] = {}
    for cfg in configs:
        configs_by_provider.setdefault(cfg.provider, []).append(cfg)

    # 1. Admin-configured (UUID first)
    if getattr(ps, "image_provider_id", None) and ps.image_model:
        config = next((c for c in configs if c.id == ps.image_provider_id), None)
        if config:
            await ensure_not_excluded(db, config, "Image generation", user_id)
            return await _cached_provider(config), ps.image_model
    if ps.image_provider and ps.image_model:
        matching = configs_by_provider.get(ps.image_provider, [])
        if config := newest_config(matching):
            await ensure_not_excluded(db, config, "Image generation", user_id)
            return await _cached_provider(config), ps.image_model

    # 2. Fallback: first image-capable provider
    for prov_name in _IMAGE_PROVIDERS:
        matching = configs_by_provider.get(prov_name, [])
        if matching:
            model = (
                ps.image_model if ps.image_provider == prov_name else None
            ) or IMAGE_DEFAULTS.get(prov_name)
            # Azure OpenAI has no built-in image model (its ids are deployment
            # names), so it is only usable once an admin names one.
            if model:
                await ensure_not_excluded(db, matching[-1], "Image generation", user_id)
                return await _cached_provider(matching[-1]), model

    # 3. Last: the gateway's default image model
    chosen_first = sorted(reversed(configs), key=lambda c: c.id != getattr(ps, "image_provider_id", None))
    if listed := await gateway_default(chosen_first, "image"):
        config, provider, model = listed
        await ensure_not_excluded(db, config, "Image generation", user_id)
        return provider, model

    raise ValueError(
        "An image-capable provider is required for image generation. An admin must configure one in Settings "
        "and, for a provider with no default image model, choose its image model."
    )


#: Catalog modes of the models that MAKE images (an edit-only model takes one in).
_IMAGE_CATALOG_MODES = frozenset({"image_generation"})
#: Catalog mode of the models that embed text.
_EMBEDDING_CATALOG_MODES = frozenset({"embedding"})


@lru_cache(maxsize=4)
def _catalog_models_in(modes: frozenset[str]) -> frozenset[str]:
    """Lowercased ids (full key + basename) the model catalog lists in one of *modes*."""
    try:
        data = json.loads(_MODEL_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    ids: set[str] = set()
    for key, val in data.items():
        if isinstance(val, dict) and val.get("mode") in modes:
            ids.add(key.lower())
            ids.add(key.rsplit("/", 1)[-1].lower())
    return frozenset(ids)


def _catalog_lists(model_id: str | None, modes: frozenset[str]) -> bool:
    """Whether the model catalog lists *model_id* (or its basename) in one of *modes*.

    An id the catalog does not know is not one: its name decides nothing (invariant 18).
    """
    if not model_id:
        return False
    mid = model_id.lower()
    known = _catalog_models_in(modes)
    return mid in known or mid.rsplit("/", 1)[-1] in known


def is_image_model(model_id: str | None) -> bool:
    """Whether *model_id* generates images, as the model catalog's ``mode`` declares it."""
    return _catalog_lists(model_id, _IMAGE_CATALOG_MODES)


def is_embedding_model(model_id: str | None) -> bool:
    """Whether *model_id* embeds text, as the model catalog's ``mode`` declares it."""
    return _catalog_lists(model_id, _EMBEDDING_CATALOG_MODES)


def provider_capabilities(provider_type: str) -> list[str]:
    """What a provider type is known to serve, from its class: ``chat`` (or its one capability) and ``decision``.

    A single-capability provider (``only_capability``: an embedding server, a
    voice generator) serves that one and never chats; any other chats, and
    serves decisions when it speaks a decisions API. Settings offers a
    provider's chat models only when it chats. Its image, video, speech and
    embedding models are discovered by asking it, in each capability's section:
    an OpenAI-compatible class implements them all whatever its endpoint serves.
    """
    cls = PROVIDER_MAP.get(provider_type)
    if cls is None:
        return []
    if only := getattr(cls, "only_capability", None):
        return [only]
    return ["chat", "decision"] if getattr(cls, "decisions_api", None) else ["chat"]


# ---------------------------------------------------------------------------
# Video generation
# ---------------------------------------------------------------------------

#: The video model a provider starts with when an admin names none, as each
#: provider class declares it (``default_video_model``). The video-capable
#: providers that declare none (an Azure OpenAI deployment, a LiteLLM proxy, a
#: custom OpenAI-compatible endpoint, the gateway) serve the models their admin
#: deployed: they are used once a model is chosen in Settings, or named by the
#: caller. ElevenLabs declares none either: added for its voices, it never
#: becomes the platform's video generator on its own.
VIDEO_DEFAULTS: dict[str, str] = {
    name: cls.default_video_model for name, cls in PROVIDER_MAP.items() if getattr(cls, "default_video_model", None)
}
#: A provider that serves no chat was added for its media (Kling AI, Runway, Seedance): it generates first.
_VIDEO_PROVIDERS = tuple(
    sorted(VIDEO_DEFAULTS, key=lambda name: getattr(PROVIDER_MAP[name], "only_capability", None) is None)
)

#: Id prefixes of the video families, for an id the catalog does not know
#: (a newer snapshot, a gateway alias that keeps the family name).
_VIDEO_MODEL_PREFIXES = ("sora-", "veo-", "gen3a_", "gen4_", "gen4-", "ray-2", "ray2")


@lru_cache(maxsize=1)
def _catalog_video_models() -> frozenset[str]:
    """Lowercased ids (full key + basename) the model catalog lists as ``video_generation``."""
    try:
        data = json.loads(_MODEL_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    ids: set[str] = set()
    for key, val in data.items():
        if isinstance(val, dict) and val.get("mode") == "video_generation":
            ids.add(key.lower())
            ids.add(key.rsplit("/", 1)[-1].lower())
    return frozenset(ids)


def is_video_model(model_id: str | None) -> bool:
    """Whether *model_id* is a video generation model: the catalog's mode, else its family prefix."""
    if not model_id:
        return False
    mid = model_id.lower()
    basename = mid.rsplit("/", 1)[-1]
    if mid in _catalog_video_models() or basename in _catalog_video_models():
        return True
    return basename.startswith(_VIDEO_MODEL_PREFIXES)


async def _video_model_retired(provider: BaseLLMProvider, model: str) -> str | None:
    """Why *model* no longer makes videos on *provider*, or ``None`` (not known to be retired)."""
    try:
        return await asyncio.wait_for(provider.video_model_unavailable(model), timeout=10)
    except Exception as exc:
        logger.debug("Video model %s lifecycle check failed: %s", model, format_exception_message(exc))
        return None


class VideoModelUnusableError(ValueError):
    """A video model that makes no video on the provider it resolved to: not served there, or retired.

    ``reason`` names the model and why; ``not_served`` tells the first from the second.
    """

    def __init__(self, reason: str, *, not_served: bool = False) -> None:
        super().__init__(f"{reason}: pick another video model.")
        self.reason = reason
        self.not_served = not_served


async def _video_model_unusable(provider: Any, config: Any, model: str) -> VideoModelUnusableError | None:
    """Why *model* makes no video on *config*'s *provider*: not served (``video_served``), else retired; or ``None``."""
    if unserved := await video_served.problem(provider, config, model):
        return VideoModelUnusableError(unserved, not_served=True)
    if retired := await _video_model_retired(provider, model):
        return VideoModelUnusableError(retired)
    return None


async def platform_video_model_problem(db: AsyncSession) -> str | None:
    """Why the video model chosen in Settings can no longer make a video, or ``None`` (usable, or none chosen).

    The same check :func:`get_video_provider` runs before using it, so
    Settings never shows a retired model as active.
    """
    ps = await _get_platform_settings(db)
    model = getattr(ps, "video_model", None)
    provider_id = getattr(ps, "video_provider_id", None)
    if not model or not provider_id:
        return None
    result = await db.execute(select_provider_configs())
    config = next((c for c in result.scalars().all() if c.id == provider_id), None)
    if config is None:
        return f"The provider {model} was chosen on is no longer configured"
    unusable = await _video_model_unusable(await _cached_provider(config), config, model)
    return unusable.reason if unusable is not None else None


def _video_model_on(config: LLMProviderConfig, ps: Any, model: str | None) -> str | None:
    """The model a video is asked of on *config*: the one named, else the platform's there, else the built-in one."""
    on_platform = getattr(ps, "video_provider_id", None) == config.id
    return model or (getattr(ps, "video_model", None) if on_platform else None) or VIDEO_DEFAULTS.get(config.provider)


#: Why a video model several providers list is asked of the platform's provider (:func:`_shared_model_provider`).
PLATFORM_VIDEO_CHOICE = "the provider chosen in Settings > AI and models > Media > Video generation"


def _shared_model_provider(
    serving: list[tuple[LLMProviderConfig, Any]], prefer: Sequence[tuple[object, str]], platform_provider_id: object
) -> tuple[tuple[LLMProviderConfig, Any], str] | None:
    """The provider a video model *serving* lists several of is asked of, and why: the first of *prefer*
    ``(provider id, why)`` among them - the caller's own choices for that model, a video's shots made with it - then
    the platform's chosen video provider (:data:`PLATFORM_VIDEO_CHOICE`); ``None`` when neither is among them."""
    by_id = {str(config.id): (config, provider) for config, provider in serving}
    for wanted, why in [*prefer, (platform_provider_id, PLATFORM_VIDEO_CHOICE)]:
        if wanted is not None and str(wanted) in by_id:
            return by_id[str(wanted)], why
    return None


async def declared_video_reference_limits(
    db: AsyncSession,
    *,
    provider_id: object = None,
    model: str | None = None,
    user_id: object = None,
    prefer: Sequence[tuple[object, str]] = (),
) -> tuple[str, dict[str, Any]] | None:
    """The model a video would be asked of first and what it takes beside its prompt (:func:`video_reference_limits`).

    Read as :func:`get_video_provider` picks it - the provider named, else the
    one chosen in Settings - without asking the provider anything (no
    lifecycle check: the generation still resolves through
    :func:`get_video_provider`), so a combination the model cannot take is
    refused before a row is locked or a provider is called. ``None``
    when only that resolution can tell: no provider named or chosen, an
    unknown provider, no model to name - and a provider that excludes
    *user_id*: its access check runs first, as in
    :func:`get_video_provider`, so a refusal never names a model the caller
    may not use; the resolution then refuses with the usual access error. A
    model named alone is read on the provider listing it
    (:func:`_offering`), the one chosen in Settings when none does; when
    several list it, on the one :func:`get_video_provider` asks (*prefer*,
    then the platform's: :func:`_shared_model_provider`), ``None`` when it
    asks none of them.
    """
    ps = await _get_platform_settings(db)
    platform_provider_id = getattr(ps, "video_provider_id", None)
    result = await db.execute(select_provider_configs())
    configs = list(result.scalars().all())
    if provider_id:
        wanted = str(provider_id)
    elif model and model != getattr(ps, "video_model", None):
        serving, unlisted, refused = await _offering(db, user_id, configs, model)
        if len(serving) == 1:
            return model, video_reference_limits(serving[0][1], model)
        if len(serving) > 1 and (shared := _shared_model_provider(serving, prefer, platform_provider_id)) is not None:
            return model, video_reference_limits(shared[0][1], model)
        if serving or refused or not platform_provider_id or all(str(c.id) != str(platform_provider_id) for c in unlisted):
            return None
        wanted = str(platform_provider_id)
    elif platform_provider_id and getattr(ps, "video_model", None):
        wanted = str(platform_provider_id)
    else:
        return None
    config = next((c for c in configs if str(c.id) == wanted), None)
    chosen = _video_model_on(config, ps, model) if config is not None else None
    if not chosen:
        return None
    try:
        await ensure_not_excluded(db, config, "Video generation", user_id)
    except ProviderNotUsableError:
        return None
    return chosen, video_reference_limits(await _cached_provider(config), chosen)


async def get_video_provider(
    db: AsyncSession,
    *,
    user_id: object = None,
    provider_id: object = None,
    model: str | None = None,
    prefer: Sequence[tuple[object, str]] = (),
    chose: list[str] | None = None,
) -> tuple[BaseLLMProvider, str, LLMProviderConfig]:
    """The provider, model and config a video is generated with.

    Resolution:
    1. The caller's choice (*provider_id*, and *model* or the platform's model
       on that provider, else the provider's default).
    2. A *model* named alone is asked of the provider that lists it
       (:func:`_video_listings`: the table ``video_studio_capabilities``
       shows), never of another one. A model several providers list is asked
       of the first of *prefer* ``(provider id, why)`` among them - what the
       caller knows of that model: a video's shots made with it, the shot's
       own provider -, else of the platform's chosen video provider,
       the reason said in *chose*; refused until one is named when neither
       lists it (:func:`_shared_model_provider`). One no provider lists is
       asked only of a provider that makes videos and whose listing is empty
       or failed (a deployment it does not list) - the one chosen in Settings
       first.
    3. Admin-configured ``video_provider_id`` / ``video_model`` from
       ``PlatformSettings`` (Settings > AI and models > Media > Video generation).
    4. Fallback: the first provider with a built-in video model
       (:data:`VIDEO_DEFAULTS`), then the first one listing a video model.

    A model its provider has retired, or does not serve - it answered
    HTTP 404 for it, to a probe or a generation (``video_served``) -,
    is never used: named by the caller or chosen by the platform, it is
    refused with the reason (:class:`VideoModelUnusableError`) - never handed
    to another model or provider (one model per round); only a
    provider's built-in model, with nothing chosen, gives way to the next
    live one, and with none left the error says why each was skipped. Never
    for a person the resolved provider excludes.
    """
    ps = await _get_platform_settings(db)
    result = await db.execute(select_provider_configs())
    configs = list(result.scalars().all())
    platform_model = getattr(ps, "video_model", None)
    platform_provider_id = getattr(ps, "video_provider_id", None)
    skipped: list[str] = []

    def label(config: LLMProviderConfig) -> str:
        return getattr(config, "display_name", None) or config.provider

    if provider_id:
        wanted = str(provider_id)
        config = next((c for c in configs if str(c.id) == wanted), None)
        if config is None:
            raise ValueError("No video provider with this id is configured.")
        chosen = _video_model_on(config, ps, model)
        if not chosen:
            raise ValueError(f"Name the video model to use on {label(config)}.")
        await ensure_not_excluded(db, config, "Video generation", user_id)
        provider = await _cached_provider(config)
        if (unusable := await _video_model_unusable(provider, config, chosen)) is not None:
            raise unusable
        return provider, chosen, config

    if model:
        serving, unlisted, refused = await _offering(db, user_id, configs, model)
        offered = ", ".join(label(c) for c, _p in serving)
        if len(serving) > 1:
            shared = _shared_model_provider(serving, prefer, platform_provider_id)
            if shared is None:
                raise ValueError(
                    f"The video model {model} is offered by several providers ({offered}): name the one to use with "
                    "its provider_id (video_studio_capabilities lists each model with its provider_id)."
                )
            (config, provider), why = shared
            if chose is not None:
                chose.append(
                    f"{model} is offered by {offered}: asked of {label(config)}, {why} - another one is named with "
                    "provider_id."
                )
        elif serving:
            config, provider = serving[0]
        elif refused:
            # Every provider listing it answered that it does not serve it: never asked of another one.
            raise refused[0]
        else:
            deployed = [config for config in unlisted if _makes_videos(config)]
            if not deployed:
                raise ValueError(
                    f"No video provider you can use offers the model {model}: video_studio_capabilities lists the "
                    "video models you can use."
                )
            config = next((c for c in deployed if c.id == platform_provider_id), deployed[0])
            provider = await _cached_provider(config)
        await ensure_not_excluded(db, config, "Video generation", user_id)
        if (unusable := await _video_model_unusable(provider, config, model)) is not None:
            raise unusable
        return provider, model, config

    if platform_provider_id and platform_model:
        config = next((c for c in configs if c.id == platform_provider_id), None)
        if config is None:
            # The platform's choice is the model every shot asking none is made on: said, never replaced.
            raise VideoModelUnusableError(
                f"The provider {platform_model} was chosen on is no longer configured (the platform's choice in "
                "Settings > AI and models > Media > Video generation)"
            )
        await ensure_not_excluded(db, config, "Video generation", user_id)
        provider = await _cached_provider(config)
        chosen = _video_model_on(config, ps, model)
        unusable = await _video_model_unusable(provider, config, chosen)
        if unusable is None:
            return provider, chosen, config
        raise VideoModelUnusableError(
            f"{unusable.reason} (the platform's choice in Settings > AI and models > Media > Video generation, on {label(config)})",
            not_served=unusable.not_served,
        )

    for prov_name in _VIDEO_PROVIDERS:
        for config in reversed([c for c in configs if c.provider == prov_name]):
            provider = await _cached_provider(config)
            chosen = model or VIDEO_DEFAULTS[prov_name]
            if (unusable := await _video_model_unusable(provider, config, chosen)) is not None:
                skipped.append(f"{unusable.reason} (on {label(config)})")
                continue
            await ensure_not_excluded(db, config, "Video generation", user_id)
            return provider, chosen, config

    if not model:
        from app.llm.provider_access import is_excluded, load_provider_audience

        audience = await load_provider_audience(db, user_id)
        for config in configs:
            if config.provider in _VIDEO_PROVIDERS or is_excluded(config, audience):
                continue
            provider = await _cached_provider(config)
            try:
                listed = await asyncio.wait_for(provider.list_video_models(), timeout=15)
            except Exception as exc:
                logger.info("Video models of %s not listed: %s", config.provider, format_exception_message(exc))
                continue
            unserved = await video_served.problems(provider, config, list(listed or []))
            if served := [listed_model for listed_model in listed or [] if listed_model not in unserved]:
                return provider, served[0], config

    if skipped:
        raise ValueError(
            "No video model is available: "
            + "; ".join(skipped)
            + ". An admin adds a provider whose video models are live and picks it in Settings > AI and models > Media > "
            "Video generation."
        )
    raise ValueError(
        "No video generation model is configured. An admin chooses one in Settings > AI and models > Media > Video generation."
    )


async def list_video_models_for(db: AsyncSession, *, user_id: object) -> list[dict[str, Any]]:
    """Every video model the configured providers offer *user_id* and serve (:func:`video_model_listing`)."""
    return (await video_model_listing(db, user_id=user_id))[0]


async def video_model_listing(db: AsyncSession, *, user_id: object) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(served, not_served)``: the video models the configured providers offer *user_id*, and those a provider
    lists but no longer serves.

    A served model is ``{model, provider, provider_id, label, reference_images,
    reference_images_with_start_frame}``: how many reference images it takes
    beside its prompt, as its provider declares it. One its provider
    answered HTTP 404 for - its probe, or a generation (``video_served``) - is
    not among them: ``{model, provider, provider_id, label, reason}`` says
    why. A provider that excludes the person offers nothing; a provider whose
    listing fails is skipped (logged), so one unreachable endpoint never hides
    the others.
    """
    result = await db.execute(select_provider_configs())
    listings = await _video_listings(db, user_id, list(result.scalars().all()))
    refusals = await asyncio.gather(
        *(video_served.problems(provider, config, list(models or [])) for config, provider, models in listings)
    )
    served: list[dict[str, Any]] = []
    not_served: list[dict[str, Any]] = []
    for (config, provider, models), unserved in zip(listings, refusals, strict=True):
        named = {
            "provider": config.provider,
            "provider_id": str(config.id),
            "label": getattr(config, "display_name", None) or config.provider,
        }
        for model_id in models or []:
            if model_id in unserved:
                not_served.append({"model": model_id, **named, "reason": unserved[model_id]})
            else:
                served.append({"model": model_id, **named, **video_reference_listing(provider, model_id)})
    return served, not_served


async def _video_listings(
    db: AsyncSession, user_id: object, configs: list[LLMProviderConfig]
) -> list[tuple[LLMProviderConfig, Any, list[str] | None]]:
    """Each provider of *configs* *user_id* may use, with the video models it lists (``None``: its listing failed).

    Listed together (each bounded to 15 s), in the order of *configs*; a
    provider that excludes the person is left out, one whose listing
    fails is logged.
    """
    from app.llm.provider_access import is_excluded, load_provider_audience

    audience = await load_provider_audience(db, user_id)

    async def listed(config: LLMProviderConfig) -> tuple[LLMProviderConfig, Any, list[str] | None]:
        provider = None
        try:
            provider = await _cached_provider(config)
            return config, provider, list(await asyncio.wait_for(provider.list_video_models(), timeout=15))
        except Exception as exc:
            logger.info("Video models of %s not listed: %s", config.provider, format_exception_message(exc))
            return config, provider, None

    return list(await asyncio.gather(*(listed(config) for config in configs if not is_excluded(config, audience))))


def _makes_videos(config: LLMProviderConfig) -> bool:
    """Whether the provider class of *config* generates videos at all (it implements ``generate_video``)."""
    cls = PROVIDER_MAP.get(config.provider)
    return cls is not None and cls.generate_video is not BaseLLMProvider.generate_video


async def _offering(
    db: AsyncSession, user_id: object, configs: list[LLMProviderConfig], model: str
) -> tuple[list[tuple[LLMProviderConfig, Any]], list[LLMProviderConfig], list[VideoModelUnusableError]]:
    """``(serving, unlisted, refused)``: the providers *user_id* may use that list the video *model* and serve it,
    with their instance; those whose listing is empty or failed - which may serve it as a deployment they do
    not list -; and why each provider listing it does not serve it (``video_served``).
    """
    listings = await _video_listings(db, user_id, configs)
    listing = [(config, provider) for config, provider, listed in listings if listed and model in listed]
    reasons = await asyncio.gather(*(video_served.problem(provider, config, model) for config, provider in listing))
    serving = [pair for pair, reason in zip(listing, reasons, strict=True) if not reason]
    refused = [VideoModelUnusableError(reason, not_served=True) for reason in reasons if reason]
    return serving, [config for config, _provider, listed in listings if not listed], refused


def video_reference_limits(provider: Any, model: str) -> dict[str, Any]:
    """What a video model takes beside its prompt: its reference images, and with a start frame or not."""
    return {
        "reference_images": int(provider.max_reference_images(model)),
        "reference_images_with_start_frame": bool(provider.reference_images_with_start_frame(model)),
    }


def video_reference_listing(provider: Any, model: str) -> dict[str, Any]:
    """:func:`video_reference_limits` as ``video_studio_capabilities`` lists a model, with its reference constraints.

    ``reference_constraints``: the aspect ratio range, sides, formats
    and count of a reference image or an ingredient the model takes, or
    ``"unverified"`` where its provider documents none (nothing is refused
    on our side then); absent for a model that takes no reference image.
    """
    listed = video_reference_limits(provider, model)
    if not listed["reference_images"]:
        return listed
    declared = getattr(provider, "reference_image_limits", None)
    limits = declared(model) if callable(declared) else None
    return {**listed, "reference_constraints": limits.as_dict() if limits is not None else "unverified"}


# ---------------------------------------------------------------------------
# Speech generation
# ---------------------------------------------------------------------------

#: The speech model a provider starts with when an admin names none, as each
#: provider class declares it (``default_speech_model``). The speech-capable
#: providers that declare none (an Azure OpenAI deployment, a LiteLLM proxy, a
#: custom OpenAI-compatible endpoint, the gateway) speak once a model is
#: chosen in Settings, or named by the caller.
SPEECH_DEFAULTS: dict[str, str] = {
    name: cls.default_speech_model for name, cls in PROVIDER_MAP.items() if getattr(cls, "default_speech_model", None)
}
#: The order the fallback tries them in: a provider configured for speech
#: alone first (it was added for its voices), then Settings' provider order.
_SPEECH_PROVIDERS = tuple(
    sorted(SPEECH_DEFAULTS, key=lambda name: getattr(PROVIDER_MAP[name], "only_capability", None) != "speech")
)

#: Id markers of the speech families, for an id the catalog does not know.
_SPEECH_MODEL_MARKERS = ("tts",)


@lru_cache(maxsize=1)
def _catalog_speech_models() -> frozenset[str]:
    """Lowercased ids (full key + basename) the model catalog lists as ``audio_speech``."""
    try:
        data = json.loads(_MODEL_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    ids: set[str] = set()
    for key, val in data.items():
        if isinstance(val, dict) and val.get("mode") == "audio_speech":
            ids.add(key.lower())
            ids.add(key.rsplit("/", 1)[-1].lower())
    return frozenset(ids)


def is_speech_model(model_id: str | None) -> bool:
    """Whether *model_id* is a text-to-speech model: the catalog's mode, else a ``tts`` marker."""
    if not model_id:
        return False
    mid = model_id.lower()
    basename = mid.rsplit("/", 1)[-1]
    if mid in _catalog_speech_models() or basename in _catalog_speech_models():
        return True
    return any(marker in basename for marker in _SPEECH_MODEL_MARKERS)


@dataclass(frozen=True)
class SpeechTarget:
    """What a text is spoken with."""

    provider: BaseLLMProvider
    model: str
    config: LLMProviderConfig
    #: The admin's voice when this is the admin's model; ``None`` lets the
    #: caller's voice, else the provider's first voice, speak.
    voice: str | None = None


def _speech_refusal(routing, config: LLMProviderConfig) -> ProviderNotUsableError:
    label = getattr(config, "display_name", None) or config.provider
    if str(config.id) in {str(i) for i in routing.excluded_ids}:
        return ProviderNotUsableError(f"Speech generation on {label} is not available to you.")
    return ProviderNotUsableError(f"{label} is reserved for other groups: it cannot speak for you.")


async def get_speech_provider(
    db: AsyncSession,
    *,
    user_id: object = None,
    provider_id: object = None,
    model: str | None = None,
) -> SpeechTarget:
    """The provider, model and voice a text is spoken with.

    Resolution:
    1. The caller's choice (*provider_id*, and *model* or the platform's model
       on that provider, else the provider's default).
    2. Admin-configured ``speech_provider_id`` / ``speech_model`` /
       ``speech_voice`` from ``PlatformSettings`` (Settings > AI and models > Media >
       Speech generation).
    3. Fallback: the first provider with a built-in speech model
       (:data:`SPEECH_DEFAULTS`), the person's team's first.
    4. Last: the default speech model of the gateway the admin chose, else of
       the person's team's first (:func:`gateway_default`), with the
       platform's voice when it offers it, else its first voice.

    Only among the providers the person may use: one reserved for teams they
    are not in, or one that excludes them, never speaks for them
    #(4684). An admin's choice they may not use falls back to step 3.
    """
    ps = await _get_platform_settings(db)
    result = await db.execute(select_provider_configs())
    configs = list(result.scalars().all())
    routing = await provider_routing(db, user_id, configs)
    usable = list(routing.team_first)
    usable_ids = {c.id for c in usable}
    platform_model = getattr(ps, "speech_model", None)
    platform_provider_id = getattr(ps, "speech_provider_id", None)
    platform_voice = getattr(ps, "speech_voice", None) or None

    def _voice_for(config: LLMProviderConfig, chosen: str) -> str | None:
        return platform_voice if config.id == platform_provider_id and chosen == platform_model else None

    if provider_id:
        wanted = str(provider_id)
        config = next((c for c in configs if str(c.id) == wanted), None)
        if config is None:
            raise ValueError("No speech provider with this id is configured.")
        if config.id not in usable_ids:
            raise _speech_refusal(routing, config)
        chosen = model or (platform_model if platform_provider_id == config.id else None) or SPEECH_DEFAULTS.get(
            config.provider
        )
        if not chosen and (listed := await gateway_default([config], "speech")):
            _, provider, chosen = listed
            return SpeechTarget(provider, chosen, config, await _offered_voice(provider, chosen, platform_voice))
        if not chosen:
            raise ValueError(f"Name the speech model to use on {getattr(config, 'display_name', None) or config.provider}.")
        return SpeechTarget(await _cached_provider(config), chosen, config, _voice_for(config, chosen))

    if platform_provider_id and platform_model:
        config = next((c for c in usable if c.id == platform_provider_id), None)
        if config is not None:
            chosen = model or platform_model
            return SpeechTarget(await _cached_provider(config), chosen, config, _voice_for(config, chosen))

    for prov_name in _SPEECH_PROVIDERS:
        config = next((c for c in usable if c.provider == prov_name), None)
        if config is not None:
            return SpeechTarget(await _cached_provider(config), model or SPEECH_DEFAULTS[prov_name], config)

    if listed := await gateway_default(sorted(usable, key=lambda c: c.id != platform_provider_id), "speech"):
        config, provider, chosen = listed
        chosen = model or chosen
        return SpeechTarget(provider, chosen, config, await _offered_voice(provider, chosen, platform_voice))

    raise ValueError(
        "No speech model is available to you. An admin chooses one in Settings > AI and models > Media > Speech generation."
    )


async def _offered_voice(provider: BaseLLMProvider, model: str, voice: str | None) -> str | None:
    """*voice* when *model* offers it, else its first voice (``pick_voice``); kept as is when none are listed."""
    if not voice:
        return None
    from app.llm.speech import pick_voice

    try:
        voices = await provider.list_voices(model)
    except Exception as exc:
        logger.info("Voices of %s not listed: %s", model, format_exception_message(exc))
        return voice
    return pick_voice(voices, voice, None, [])


async def list_speech_models_for(db: AsyncSession, *, user_id: object) -> list[dict[str, str]]:
    """Every speech model the providers *user_id* may use offer: ``[{model, provider, provider_id, label}]``.

    A provider whose listing fails is skipped (logged), so one unreachable
    endpoint never hides the others.
    """
    result = await db.execute(select_provider_configs())
    routing = await provider_routing(db, user_id, list(result.scalars().all()))
    found: list[dict[str, str]] = []
    for config in routing.team_first:
        try:
            provider = await _cached_provider(config)
            models = await asyncio.wait_for(provider.list_speech_models(), timeout=15)
        except Exception as exc:
            logger.info("Speech models of %s not listed: %s", config.provider, format_exception_message(exc))
            continue
        for model_id in models:
            found.append(
                {
                    "model": model_id,
                    "provider": config.provider,
                    "provider_id": str(config.id),
                    "label": getattr(config, "display_name", None) or config.provider,
                }
            )
    return found


async def list_voices_for(
    db: AsyncSession, *, user_id: object, provider_id: object = None, model: str | None = None
) -> tuple[SpeechTarget, list]:
    """The voices of the speech model *user_id* would be spoken with (``app.llm.speech.Voice``)."""
    target = await get_speech_provider(db, user_id=user_id, provider_id=provider_id, model=model)
    voices = await asyncio.wait_for(target.provider.list_voices(target.model), timeout=20)
    return target, list(voices)


# ---------------------------------------------------------------------------
# Music generation
# ---------------------------------------------------------------------------

#: The music model a provider starts with when an admin names none, as each
#: provider class declares it (``default_music_model``).
MUSIC_DEFAULTS: dict[str, str] = {
    name: cls.default_music_model for name, cls in PROVIDER_MAP.items() if getattr(cls, "default_music_model", None)
}
#: The order the fallback tries them in: a provider that serves no chat first
#: (it was added for its media), then Settings' provider order.
_MUSIC_PROVIDERS = tuple(
    sorted(MUSIC_DEFAULTS, key=lambda name: getattr(PROVIDER_MAP[name], "only_capability", None) is None)
)
#: Id prefixes of the music families: no catalog mode marks them.
_MUSIC_MODEL_PREFIXES = ("lyria", "music_v", "music-", "stable-audio")


def is_music_model(model_id: str | None) -> bool:
    """Whether *model_id* is a music generation model, by its family prefix."""
    if not model_id:
        return False
    return model_id.lower().rsplit("/", 1)[-1].startswith(_MUSIC_MODEL_PREFIXES)


def _music_refusal(routing, config: LLMProviderConfig) -> ProviderNotUsableError:
    label = getattr(config, "display_name", None) or config.provider
    if str(config.id) in {str(i) for i in routing.excluded_ids}:
        return ProviderNotUsableError(f"Music generation on {label} is not available to you.")
    return ProviderNotUsableError(f"{label} is reserved for other groups: it cannot compose for you.")


async def get_music_provider(
    db: AsyncSession,
    *,
    user_id: object = None,
    provider_id: object = None,
    model: str | None = None,
) -> tuple[BaseLLMProvider, str, LLMProviderConfig]:
    """The provider, model and config a track is composed with.

    Resolution:
    1. The caller's choice (*provider_id*, and *model* or the platform's model
       on that provider, else the provider's default).
    2. Admin-configured ``music_provider_id`` / ``music_model`` from
       ``PlatformSettings`` (Settings > AI and models > Media > Music generation).
    3. Fallback: the first provider with a built-in music model
       (:data:`MUSIC_DEFAULTS`), the person's team's first.

    Only among the providers the person may use, like speech:
    an admin's choice they may not use falls back to step 3.
    """
    ps = await _get_platform_settings(db)
    result = await db.execute(select_provider_configs())
    configs = list(result.scalars().all())
    routing = await provider_routing(db, user_id, configs)
    usable = list(routing.team_first)
    usable_ids = {c.id for c in usable}
    platform_model = getattr(ps, "music_model", None)
    platform_provider_id = getattr(ps, "music_provider_id", None)

    if provider_id:
        wanted = str(provider_id)
        config = next((c for c in configs if str(c.id) == wanted), None)
        if config is None:
            raise ValueError("No music provider with this id is configured.")
        if config.id not in usable_ids:
            raise _music_refusal(routing, config)
        chosen = model or (platform_model if platform_provider_id == config.id else None) or MUSIC_DEFAULTS.get(
            config.provider
        )
        if not chosen:
            raise ValueError(f"Name the music model to use on {getattr(config, 'display_name', None) or config.provider}.")
        return await _cached_provider(config), chosen, config

    if platform_provider_id and platform_model:
        config = next((c for c in usable if c.id == platform_provider_id), None)
        if config is not None:
            return await _cached_provider(config), model or platform_model, config

    for prov_name in _MUSIC_PROVIDERS:
        config = next((c for c in usable if c.provider == prov_name), None)
        if config is not None:
            return await _cached_provider(config), model or MUSIC_DEFAULTS[prov_name], config

    raise ValueError(
        "No music model is available to you. An admin adds a provider that composes music and picks it in "
        "Settings > AI and models > Media > Music generation."
    )


async def list_music_models_for(db: AsyncSession, *, user_id: object) -> list[dict[str, Any]]:
    """Every music model the providers *user_id* may use offer: ``[{model, provider, provider_id, label, continues}]``,
    ``continues`` whether it composes the continuation of a reference clip (``continues_music``).

    A provider whose listing fails is skipped (logged), so one unreachable
    endpoint never hides the others.
    """
    result = await db.execute(select_provider_configs())
    routing = await provider_routing(db, user_id, list(result.scalars().all()))
    found: list[dict[str, str]] = []
    for config in routing.team_first:
        try:
            provider = await _cached_provider(config)
            models = await asyncio.wait_for(provider.list_music_models(), timeout=15)
        except Exception as exc:
            logger.info("Music models of %s not listed: %s", config.provider, format_exception_message(exc))
            continue
        for model_id in models:
            found.append(
                {
                    "model": model_id,
                    "provider": config.provider,
                    "provider_id": str(config.id),
                    "label": getattr(config, "display_name", None) or config.provider,
                    "continues": provider.continues_music(model_id),
                }
            )
    return found
