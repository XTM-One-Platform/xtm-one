# SPDX-License-Identifier: Apache-2.0

"""OpenAI LLM provider with full tool-calling support."""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import openai
from openai import AsyncOpenAI

from app.core.utils import format_exception_message
from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    build_openai_payload,
    parse_tool_arguments,
    retry_llm_call,
    usage_sink_of,
)
from app.llm.decisions import (
    DECISIONS_OPENAI,
    claim_decision_round,
    recall_decision_support,
    release_decision_round,
    remember_decision_support,
)
from app.llm.model_capabilities import build_openai_generation_params, uses_responses_api_with_tools
from app.llm.openai_compat import (
    consume_openai_stream_with_tools,
    content_filter_finish,
    create_with_effort_retry,
    declined_by_filter,
    endpoint_of,
    extract_openai_response_usage,
    extract_openai_usage,
    is_retryable_openai,
    normalize_openai_reasoning_effort,
    openai_function_tools,
    openai_reasoning_text,
)
from app.llm.openai_responses import responses_chat
from app.llm.speech import OpenAISpeechMixin
from app.llm.video import OpenAIVideosMixin
from app.tools.base import ToolCall, ToolDefinition

logger = logging.getLogger(__name__)

# Listing the decision models (``list_decision_models``) asks OpenAI about each
# model; a settings page waits on it, so the whole round has a deadline and
# each request its own short timeout (the client's is sized for chat). The
# model listing and the control are capped so the probes keep 3 s of the 8.
DECISION_PROBE_DEADLINE_SECONDS = 8.0
DECISION_LISTING_BUDGET_SECONDS = 3.0
DECISION_CONTROL_BUDGET_SECONDS = 2.0
_DECISION_PROBE_TIMEOUT_SECONDS = 5.0
_DECISION_PROBE_CONCURRENCY = 8


def _decision_probe_body(model: str) -> dict:
    """A choice question OpenAI refuses for its two identical choices, once it knows *model*."""
    return {
        "model": model,
        "input": "probe",
        "questions": [
            {
                "type": "choice",
                "name": "label",
                "instructions": "Pick one.",
                "choices": [
                    {"value": "a", "description": "a"},
                    {"value": "a", "description": "a"},
                ],
            }
        ],
    }


def _serves_decisions(error: Exception | None) -> bool | None:
    """What a probe's outcome says of its model; ``None`` when it says nothing."""
    if error is None:
        # Answered: the question ran, so the model serves decisions.
        return True
    if not isinstance(error, openai.APIStatusError):
        return None
    if error.code == "model_not_found":
        return False
    if error.status_code == 400 and str(error.param or "").startswith("questions"):
        return True
    return None


def _refuses_decisions(error: Exception | None) -> bool:
    """Whether the control's refusal says the key reaches no Decisions API at all (denied, or no such route)."""
    return isinstance(error, openai.APIStatusError) and error.status_code in (401, 403, 404)


async def _send_decision_probe(client: AsyncOpenAI, model: str) -> Exception | None:
    """The probe's error, ``None`` when OpenAI answered it."""
    try:
        await client.post("/decisions", cast_to=object, body=_decision_probe_body(model))
    except Exception as exc:  # classified by ``_serves_decisions``
        return exc
    return None


#: Kept referenced: the event loop holds only weak references to its tasks.
_decision_bookkeeping: set[asyncio.Task[None]] = set()


def _record_decision_round(
    config_id: str | None, scope: str, token: str, found: dict[str, bool] | None
) -> None:
    """Remember the round's answers, then end it, off the caller's path.

    Only the probes count against the listing's deadline: a slow Redis then
    neither cuts a listing short nor loses what the round learnt, and a
    cancelled caller still ends its round.
    """

    async def record() -> None:
        if found:
            await remember_decision_support(config_id, scope, found)
        await release_decision_round(config_id, scope, token)

    task = asyncio.get_running_loop().create_task(record())
    _decision_bookkeeping.add(task)
    task.add_done_callback(_decision_bookkeeping.discard)


class OpenAIProvider(OpenAISpeechMixin, OpenAIVideosMixin, BaseLLMProvider):
    """Wraps the OpenAI SDK for chat completions with tool calling (and Sora video, ``app.llm.video``; speech, ``app.llm.speech``)."""

    provider_name = "openai"
    # OpenAI serves its decision models on the Decisions API.
    decisions_api = DECISIONS_OPENAI
    # Each model of the account's listing is asked (decision_listing).
    lists_decision_models = True
    # Default chat model used by chat / chat_stream / chat_stream_with_tools
    # when no explicit model is provided.  Exposed as a class attribute so
    # the FailoverLLMProvider can resolve per-model max_output_tokens
    # overrides for fallback calls that arrive with model=None.
    default_model = "gpt-4o"
    max_output_tokens = 16_384
    default_video_model = "sora-2"
    default_speech_model = "gpt-4o-mini-tts"

    # OpenAI honours ``stream_options={"include_usage": True}`` to emit a
    # final usage chunk during streaming — required for token/cost tracking.
    supports_stream_usage: bool = True

    # ``response_format`` (json_schema) is supported on streaming chat
    # completions exactly like non-streaming ones, so the agentic loop can
    # forward ``structured_output`` to ``chat_stream()``.
    supports_streaming_structured_output: bool = True

    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(
            api_key=api_key,
            timeout=httpx.Timeout(connect=10, read=600, write=30, pool=10),
        )

    # ------------------------------------------------------------------
    # chat (with tool support)
    # ------------------------------------------------------------------

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

        oai_messages, pdf_tokens = await build_openai_payload(
            messages, "openai", context_window=self._resolve_context_window(model)
        )
        text_chars, image_count, native_file_bytes = self._measure_oai_payload(oai_messages)
        self._check_resolved_overflow(
            text_chars=text_chars, image_count=image_count,
            native_file_bytes=native_file_bytes, tools=tools, model=model,
            native_document_tokens=pdf_tokens,
        )

        if tools and uses_responses_api_with_tools(model):
            return await responses_chat(
                self.client,
                model=model,
                oai_messages=oai_messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                is_retryable=is_retryable_openai,
                **kwargs,
            )

        # --- build params ---
        params: dict = {
            "model": model,
            "messages": oai_messages,
        }
        params.update(
            build_openai_generation_params(
                model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )

        # Reasoning effort for o-series / reasoning models
        if reasoning_effort := normalize_openai_reasoning_effort(
            kwargs.get("reasoning_effort"), model, has_tools=bool(tools)
        ):
            params["reasoning_effort"] = reasoning_effort

        # Structured output: native response_format for JSON schema enforcement
        if structured_output := kwargs.get("structured_output"):
            params["response_format"] = structured_output

        # --- tools ---
        if tools:
            params["tools"] = openai_function_tools(tools, model)

        # --- call (with retry on transient errors) ---
        response = await retry_llm_call(
            lambda: create_with_effort_retry(
                lambda p: self.client.chat.completions.create(**p), params, model, endpoint=endpoint_of(self.client)
            ),
            is_retryable=is_retryable_openai,
        )

        choice = response.choices[0]
        message = choice.message
        usage = extract_openai_response_usage(response.usage)
        if declined := declined_by_filter(choice, model, usage):
            raise declined

        # --- parse tool calls ---
        tool_calls: list[ToolCall] | None = None
        if message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=parse_tool_arguments(
                            tc.function.arguments, tool_name=tc.function.name
                        ),
                    )
                )

        if choice.finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif choice.finish_reason == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"

        reasoning = openai_reasoning_text(message)

        return ChatResponse(
            content=message.content or "",
            model=response.model,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            stop_reason=stop_reason,
            thinking=reasoning if reasoning else None,
        )

    # ------------------------------------------------------------------
    # chat_stream (native OpenAI streaming)
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
        """Stream text chunks from OpenAI."""
        model = model or self.default_model
        usage_sink = usage_sink_of(kwargs)
        if usage_sink is not None:
            usage_sink.restart()

        api_messages, pdf_tokens = await build_openai_payload(
            messages, "openai", context_window=self._resolve_context_window(model)
        )
        text_chars, image_count, native_file_bytes = self._measure_oai_payload(api_messages)
        self._check_resolved_overflow(
            text_chars=text_chars, image_count=image_count,
            native_file_bytes=native_file_bytes, tools=tools, model=model,
            native_document_tokens=pdf_tokens,
        )

        extra_params: dict = {}
        if self.supports_stream_usage:
            extra_params["stream_options"] = {"include_usage": True}
        extra_params.update(
            build_openai_generation_params(
                model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )
        if reasoning_effort := normalize_openai_reasoning_effort(kwargs.get("reasoning_effort"), model):
            extra_params["reasoning_effort"] = reasoning_effort

        # Structured output: native response_format for JSON schema
        # enforcement — supported on streaming chat completions too.
        if structured_output := kwargs.get("structured_output"):
            extra_params["response_format"] = structured_output

        self.last_stop_reason = None
        self.last_usage = None
        # Retry transient errors on stream establishment only — nothing has
        # been yielded to the caller until the create() call returns.
        stream = await retry_llm_call(
            lambda: create_with_effort_retry(
                lambda p: self.client.chat.completions.create(
                    model=model, messages=api_messages, stream=True, **p
                ),
                extra_params,
                model,
            ),
            is_retryable=is_retryable_openai,
        )
        on_thinking = kwargs.get("on_thinking")
        filtered_choice = None
        # This call's own usage: ``self.last_usage`` is shared by every request
        # on the cached provider instance, so a decline must not read it back.
        usage: dict = {}

        async for chunk in stream:
            # Usage arrives in a trailing chunk with empty ``choices``;
            # capture it before the guard below skips that chunk.
            if getattr(chunk, "usage", None) is not None:
                usage = extract_openai_usage(chunk.usage)
                self.last_usage = usage
                if usage_sink is not None:
                    usage_sink.report(usage)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason is not None:
                if choice.finish_reason == "length":
                    self.last_stop_reason = "max_tokens"
                else:
                    self.last_stop_reason = "end_turn"
                filtered_choice = content_filter_finish(choice) or filtered_choice
            delta = choice.delta
            reasoning = openai_reasoning_text(delta)
            if reasoning and on_thinking:
                await on_thinking(reasoning)
            if delta.content:
                yield delta.content

        # After the loop: the usage chunk trails the finish chunk.
        if declined := declined_by_filter(filtered_choice, model, usage):
            raise declined

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

        oai_messages, pdf_tokens = await build_openai_payload(
            messages, "openai", context_window=self._resolve_context_window(model)
        )
        text_chars, image_count, native_file_bytes = self._measure_oai_payload(oai_messages)
        self._check_resolved_overflow(
            text_chars=text_chars, image_count=image_count,
            native_file_bytes=native_file_bytes, tools=tools, model=model,
            native_document_tokens=pdf_tokens,
        )
        if tools and uses_responses_api_with_tools(model):
            return await responses_chat(
                self.client,
                model=model,
                oai_messages=oai_messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                is_retryable=is_retryable_openai,
                on_token=on_token,
                stream=True,
                **kwargs,
            )
        return await consume_openai_stream_with_tools(
            self.client, model=model, oai_messages=oai_messages, tools=tools,
            on_token=on_token, temperature=temperature, max_tokens=max_tokens,
            include_usage=self.supports_stream_usage,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # embed
    # ------------------------------------------------------------------

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        from app.llm.router import EMBEDDING_DEFAULTS

        model = model or EMBEDDING_DEFAULTS.get("openai", "text-embedding-3-large")

        # Honour the platform-configured embedding dimension (Matryoshka
        # shortening). Only the ``text-embedding-3`` family accepts the
        # ``dimensions`` parameter; older models (``text-embedding-ada-002``)
        # reject it and always return their native size. Previously this was
        # never passed, so an admin who configured a non-native dimension got
        # native-sized vectors stored — leaving the KB flagged permanently
        # "stale" (stored dims never matched the configured target) and making
        # "Re-embed all" loop forever without converging.
        dims = self.embedding_dimensions
        if dims and model.startswith("text-embedding-3"):
            try:
                response = await self.client.embeddings.create(
                    input=text, model=model, dimensions=dims
                )
            except Exception as exc:
                # Some deployments / model snapshots reject the parameter.
                # Fall back to the native size and let the document processor's
                # dimension-convergence guard surface any mismatch as an
                # actionable error rather than silently corrupting the index.
                msg = str(exc).lower()
                if (
                    "dimension" in msg
                    or "unexpected keyword" in msg
                    or "unsupported" in msg
                ):
                    response = await self.client.embeddings.create(
                        input=text, model=model
                    )
                else:
                    raise
        else:
            response = await self.client.embeddings.create(input=text, model=model)
        return response.data[0].embedding

    # ------------------------------------------------------------------
    # list_models
    # ------------------------------------------------------------------

    async def list_models(self) -> list[str]:
        """Fetch available models from the OpenAI API, filtered to chat-capable models."""
        from app.llm.router import is_chat_capable_model

        try:
            response = await self.client.models.list()
            # Filter to chat-capable models (gpt-*, o1*, o3*, o4*, chatgpt-*)
            # Exclude embedding, tts, whisper, dall-e, moderation, babbage, davinci
            # and Responses-API-only (pro, codex, deep-research) models
            chat_prefixes = ("gpt-", "o1", "o3", "o4", "chatgpt-")
            exclude_keywords = (
                "realtime",
                "audio",
                "transcribe",
                "mini-tts",
                "search",
                "image",
            )
            models: list[str] = []
            for m in response.data:
                model_id = m.id
                if (
                    any(model_id.startswith(p) for p in chat_prefixes)
                    and not any(kw in model_id for kw in exclude_keywords)
                    and is_chat_capable_model(model_id)
                ):
                    models.append(model_id)
            models.sort(reverse=True)
            return models
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise
        except Exception as exc:
            # WARNING: returning a stale hardcoded list silently means
            # the admin UI shows models the live API doesn't actually
            # offer. Log so this doesn't drift undetected.
            logger.warning(
                "OpenAI list_models endpoint failed — returning hardcoded "
                "fallback list (may be stale): %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return [
                "gpt-6.1-sol",
                "gpt-6-astra",
                "gpt-6-sol",
                "gpt-6-luna",
                "gpt-5.6",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "gpt-4o",
                "gpt-4o-mini",
            ]

    # ------------------------------------------------------------------
    # list_embedding_models
    # ------------------------------------------------------------------

    async def list_embedding_models(self) -> list[str]:
        """Fetch available embedding models from the OpenAI API."""
        try:
            response = await self.client.models.list()
            models: list[str] = []
            for m in response.data:
                if m.id.startswith("text-embedding-"):
                    models.append(m.id)
            models.sort(reverse=True)
            return models
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise
        except Exception as exc:
            logger.warning(
                "OpenAI list_embedding_models endpoint failed — returning "
                "hardcoded fallback list (may be stale): %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return ["text-embedding-3-large", "text-embedding-3-small"]

    # ------------------------------------------------------------------
    # list_image_models
    # ------------------------------------------------------------------

    # Models that have been deprecated / removed from the OpenAI API and
    # must be silently remapped to the current default so that existing
    # platform configurations continue to work without requiring an admin
    # to reconfigure the image model.  Defined at class scope so that
    # ``list_image_models`` can also filter them out of the live API
    # response (otherwise OpenAI may keep returning ``dall-e-*`` from the
    # ``models`` endpoint while rejecting them on the ``images`` endpoint).
    _DEPRECATED_IMAGE_MODELS: dict[str, str] = {
        "dall-e-3": "gpt-image-1",
        "dall-e-2": "gpt-image-1",
    }

    # Deprecation WARNINGs are deduplicated per process so a stale
    # ``image_model`` config (e.g. ``"dall-e-3"`` persisted on PlatformSettings
    # before the auto-remap landed) doesn't produce a fresh WARNING on every
    # single image-generation request.  Cleared only on process restart.
    _logged_deprecated_models: set[str] = set()

    @classmethod
    def canonical_image_model(cls, model: str | None) -> str | None:
        """Return the current canonical name for *model*.

        Deprecated IDs (``dall-e-3`` / ``dall-e-2``) are remapped to their
        replacement; everything else (including ``None``) is passed
        through untouched.

        Single source of truth shared by:

        * :func:`app.api.v1.settings.update_general_assistant_config` —
          rewrites :attr:`PlatformSettings.image_model` at save time so
          the persisted value stays in sync with what the provider will
          actually call.
        * :meth:`generate_image` — silently remaps + logs a one-shot
          WARNING for legacy configs that haven't been re-saved yet.
        """
        if model and model in cls._DEPRECATED_IMAGE_MODELS:
            return cls._DEPRECATED_IMAGE_MODELS[model]
        return model

    # Default image model returned in fallback paths (live API errored or
    # filtered out every available image model). Kept in sync with the
    # ``generate_image`` default so the admin UI offers what the runtime
    # will actually use.
    _DEFAULT_IMAGE_MODEL = "gpt-image-1"

    # OpenAI's ``response_format`` parameter is only supported for the
    # legacy DALL-E models; GPT image models always return base64 and the
    # API rejects the parameter when sent. Gate it explicitly so a remap
    # to ``gpt-image-1`` doesn't carry over a parameter the new endpoint
    # will reject. See:
    # https://developers.openai.com/api/reference/resources/images
    _RESPONSE_FORMAT_SUPPORTED_PREFIXES = ("dall-e-",)

    # ``dall-e-3`` accepts ``1792x1024`` (landscape) and ``1024x1792``
    # (portrait) which are NOT valid for ``gpt-image-1``.  When the model
    # is remapped we must translate these to the closest equivalent sizes
    # that ``gpt-image-1`` does accept, otherwise the API returns 400.
    # Any size that is already valid for ``gpt-image-1`` is passed through
    # unchanged; anything else falls back to ``"auto"`` as a safe default.
    _GPT_IMAGE_1_VALID_SIZES = frozenset({"1024x1024", "1024x1536", "1536x1024", "auto"})
    _DALL_E_TO_GPT_IMAGE_1_SIZE_MAP: dict[str, str] = {
        "1792x1024": "1536x1024",  # landscape: closest gpt-image-1 equivalent
        "1024x1792": "1024x1536",  # portrait:  closest gpt-image-1 equivalent
    }

    async def list_image_models(self) -> list[str]:
        """Fetch available image generation models from the OpenAI API.

        Filters out any models in :attr:`_DEPRECATED_IMAGE_MODELS` so the
        admin UI never offers a model that ``generate_image`` would have
        to silently remap.

        Falls back to ``[_DEFAULT_IMAGE_MODEL]`` in three scenarios:
          * the OpenAI ``models`` endpoint is unreachable,
          * the endpoint returns no image models (no entry matches the
            ``dall-e-*`` / ``gpt-image-*`` prefixes), or
          * every returned image model is in the deprecation list.
        Without this guard, settings validation (which treats an empty
        ``list_image_models`` result as "provider does not support
        images") would incorrectly reject a reachable OpenAI provider
        even though ``generate_image`` would have remapped the deprecated
        IDs and worked just fine.
        """
        try:
            response = await self.client.models.list()
            image_prefixes = ("dall-e-", "gpt-image-")
            models: list[str] = []
            for m in response.data:
                if any(m.id.startswith(p) for p in image_prefixes):
                    if m.id in self._DEPRECATED_IMAGE_MODELS:
                        continue
                    models.append(m.id)
            if not models:
                return [self._DEFAULT_IMAGE_MODEL]
            models.sort(reverse=True)
            return models
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise
        except Exception as exc:
            logger.warning(
                "OpenAI list_image_models endpoint failed — returning "
                "default image model fallback (may be stale): %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return [self._DEFAULT_IMAGE_MODEL]

    # ------------------------------------------------------------------
    # list_decision_models
    # ------------------------------------------------------------------

    async def list_decision_models(self) -> list[str]:
        """The models of this account's listing that serve OpenAI's Decisions API, as far as known."""
        return (await self.decision_listing())[0]

    async def decision_listing(self) -> tuple[list[str], bool]:
        """The models of this account's listing that serve OpenAI's Decisions API, and whether that is all of them.

        Each model is asked a choice question OpenAI refuses before billing it,
        for its duplicate choice once it knows the model, as ``model_not_found``
        otherwise; a made-up id is asked first (spec 028, probe contract). The
        list is partial while a model is left unchecked: the listing failed,
        another round runs, the control said nothing, or the deadline passed.
        """
        from app.core.log_once import fingerprint

        loop = asyncio.get_running_loop()
        deadline = loop.time() + DECISION_PROBE_DEADLINE_SECONDS
        client = self.client.with_options(timeout=_DECISION_PROBE_TIMEOUT_SECONDS, max_retries=0)
        config_id = self.provider_config_id
        # Answers belong to the key and the endpoint it reaches.
        scope = fingerprint(self.client.api_key, str(self.client.base_url))
        known: dict[str, bool] = {}
        try:
            async with asyncio.timeout_at(deadline):
                async with asyncio.timeout(DECISION_LISTING_BUDGET_SECONDS):
                    listing = await client.models.list()
                candidates = sorted({m.id for m in listing.data})
                known = await recall_decision_support(config_id, scope, candidates)
        except Exception as exc:
            logger.debug(
                "Decision models of provider config %s not listed: %s",
                config_id,
                format_exception_message(exc) or type(exc).__name__,
            )
            return sorted(m for m, serves in known.items() if serves), False
        pending = [m for m in candidates if m not in known]
        listed = sorted(m for m, serves in known.items() if serves)
        if not pending:
            return listed, True
        try:
            async with asyncio.timeout_at(deadline):
                token = await claim_decision_round(config_id, scope, math.ceil(DECISION_PROBE_DEADLINE_SECONDS) + 2)
        except TimeoutError:
            return listed, False
        if token is None:
            logger.debug("Decision model check of provider config %s already running", config_id)
            return listed, False
        found: dict[str, bool] | None = None
        try:
            found, settled = await self._decision_round(client, pending, deadline)
        finally:
            _record_decision_round(config_id, scope, token, found)
        if found is None:
            return listed, settled
        logger.debug(
            "Decision model check of provider config %s: %d of %d models checked, %d serve decisions",
            config_id,
            len(found),
            len(pending),
            sum(found.values()),
        )
        return sorted(m for m, serves in {**known, **found}.items() if serves), settled

    async def _decision_round(
        self, client: AsyncOpenAI, pending: list[str], deadline: float
    ) -> tuple[dict[str, bool] | None, bool]:
        """The definite answers about *pending* (``None`` when the control stops the round), and whether they settle it.

        A control refused outright settles it with nothing to remember: the key
        reaches no Decisions API.
        """
        try:
            async with asyncio.timeout_at(deadline):
                async with asyncio.timeout(DECISION_CONTROL_BUDGET_SECONDS):
                    control = await _send_decision_probe(client, f"xtm-probe-{uuid.uuid4().hex}")
        except TimeoutError:
            control = TimeoutError("the control did not answer")
        if _serves_decisions(control) is not False:
            logger.debug(
                "Decision model check of provider config %s stopped at its control: %s",
                self.provider_config_id,
                format_exception_message(control) if control else "answered",
            )
            return None, _refuses_decisions(control)
        found = await self._check_decision_models(client, pending, deadline)
        return found, len(found) == len(pending)

    async def _check_decision_models(
        self, client: AsyncOpenAI, models: list[str], deadline: float
    ) -> dict[str, bool]:
        """The definite answers about *models* by *deadline*; a rate limit ends the round."""
        semaphore = asyncio.Semaphore(_DECISION_PROBE_CONCURRENCY)
        tasks: list[asyncio.Task] = []

        async def check(model: str) -> tuple[str, bool | None]:
            async with semaphore:
                error = await _send_decision_probe(client, model)
            if isinstance(error, openai.RateLimitError):
                for task in tasks:
                    if task is not asyncio.current_task():
                        task.cancel()
            return model, _serves_decisions(error)

        tasks.extend(asyncio.create_task(check(m)) for m in models)
        timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            done, _ = await asyncio.wait(tasks, timeout=timeout)
        finally:
            # Past the deadline, and when the caller is cancelled: no probe outlives the round.
            unfinished = [task for task in tasks if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
        found: dict[str, bool] = {}
        for task in done:
            if task.cancelled():
                continue
            model, serves = task.result()
            if serves is not None:
                found[model] = serves
        return found

    # ------------------------------------------------------------------
    # generate_image
    # ------------------------------------------------------------------

    async def generate_image(
        self,
        prompt: str,
        model: str | None = None,
        size: str = "1024x1024",
    ) -> bytes:
        """Generate an image using OpenAI's Images API and return raw bytes."""
        model = model or self._DEFAULT_IMAGE_MODEL
        # Remap deprecated model names so existing platform configurations
        # that stored an old model ID continue to work without requiring an
        # admin to manually reconfigure the image model setting.  Logged
        # at WARNING so ops can see at a glance why the configured model
        # isn't the one actually called.
        new_model = self.canonical_image_model(model)
        if new_model != model:
            if model not in self._logged_deprecated_models:
                self._logged_deprecated_models.add(model)
                logger.warning(
                    "OpenAI image model '%s' is deprecated; remapping to '%s'",
                    model,
                    new_model,
                )
            else:
                logger.debug(
                    "OpenAI image model '%s' is deprecated; remapping to '%s'",
                    model,
                    new_model,
                )
            model = new_model or model
            # The remapped model may not accept every size the legacy model
            # allowed.  Translate known-incompatible sizes to their closest
            # equivalent, or fall back to ``"auto"`` for anything else that
            # isn't in the new model's valid-size set.
            if size not in self._GPT_IMAGE_1_VALID_SIZES:
                remapped_size = self._DALL_E_TO_GPT_IMAGE_1_SIZE_MAP.get(size, "auto")
                logger.warning(
                    "Image size '%s' is not supported by '%s'; remapping to '%s'",
                    size,
                    model,
                    remapped_size,
                )
                size = remapped_size
        # ``response_format`` is only valid for legacy DALL-E. GPT image
        # models always return base64 in ``data[0].b64_json`` regardless,
        # and the API rejects the parameter when sent — so omit it for
        # the resolved model when it isn't a DALL-E variant.
        params: dict = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": 1,
        }
        if any(model.startswith(p) for p in self._RESPONSE_FORMAT_SUPPORTED_PREFIXES):
            params["response_format"] = "b64_json"
        response = await self.client.images.generate(**params)
        return base64.b64decode(response.data[0].b64_json)

    # ------------------------------------------------------------------
    # edit_image: reference pictures and a mask
    # ------------------------------------------------------------------

    #: The reference pictures a GPT image model is asked with (the API takes more; a shot list needs few).
    _GPT_IMAGE_REFERENCES = 3

    def image_reference_limit(self, model: str | None) -> int:
        """The GPT image models edit from several pictures (the DALL-E names are remapped to them)."""
        name = self.canonical_image_model(model or self._DEFAULT_IMAGE_MODEL) or ""
        return self._GPT_IMAGE_REFERENCES if name.startswith("gpt-image") else 0

    def image_takes_mask(self, model: str | None) -> bool:
        return self.image_reference_limit(model) > 0

    async def edit_image(
        self,
        prompt: str,
        images: list[tuple[bytes, str]],
        *,
        mask: tuple[bytes, str] | None = None,
        model: str | None = None,
        size: str = "1024x1024",
    ) -> bytes:
        """An image from the Images API's edits (``POST /images/edits``): the pictures, the prompt, a mask."""
        name = self.canonical_image_model(model or self._DEFAULT_IMAGE_MODEL) or model or self._DEFAULT_IMAGE_MODEL
        response = await self.client.images.edit(**self.image_edit_params(prompt, images, mask, name, size))
        return base64.b64decode(response.data[0].b64_json)

    @classmethod
    def image_edit_params(
        cls, prompt: str, images: list[tuple[bytes, str]], mask: tuple[bytes, str] | None, model: str, size: str
    ) -> dict:
        """The ``images.edit`` arguments, also sent by the AI Gateway (it forwards this request as it is)."""
        from app.llm.images import accepted_image

        files = []
        for index, (data, mime) in enumerate(images):
            accepted, accepted_mime = accepted_image(data, mime, ("image/png", "image/jpeg", "image/webp"))
            files.append((f"reference-{index + 1}.{accepted_mime.rsplit('/', 1)[-1]}", accepted, accepted_mime))
        params: dict = {"model": model, "prompt": prompt, "image": files if len(files) > 1 else files[0], "n": 1}
        if size in cls._GPT_IMAGE_1_VALID_SIZES and size != "auto":
            params["size"] = size
        if mask is not None:
            mask_data, _mime = accepted_image(mask[0], mask[1], ("image/png",))
            params["mask"] = ("mask.png", mask_data, "image/png")
        return params
