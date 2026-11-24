# SPDX-License-Identifier: Apache-2.0

"""Google Gemini LLM provider with full tool-calling support.

Uses the ``google-genai`` SDK (unified Google Generative AI client).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.core.utils import format_exception_message
from app.llm.base import (
    IMAGE_UNAVAILABLE_TEXT,
    TOOL_RESULT_IMAGE_DATA_NOTE,
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    prepare_base64_image_for_llm_async,
    prepare_image_bytes_for_llm_async,
    reads_reasoning,
    register_provider_extra_counter,
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
from app.llm.images import nearest_aspect_ratio
from app.llm.model_limits import reported_context_window
from app.tools.base import ToolCall, ToolDefinition

logger = logging.getLogger(__name__)

#: No answer from the video API at all - a reset connection, a timeout - which
#: the SDK does not always wrap in ``genai_errors.APIError``: transient.
_VIDEO_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (httpx.TransportError, TimeoutError, OSError)

#: The aspect ratios a Gemini image model draws at.
_GEMINI_IMAGE_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")


def _aspect_ratio_of(size: str) -> str:
    """The Gemini aspect ratio nearest a ``WxH`` size; ``1:1`` for anything else (``auto``)."""
    return nearest_aspect_ratio(size, _GEMINI_IMAGE_RATIOS)


def _gemini_prompt_tokens(usage_metadata: object) -> int:
    """Input tokens Google bills: the prompt plus the tool-use prompt tokens.

    ``tool_use_prompt_token_count`` counts the results of server-side tool
    executions (grounding, code execution) fed back to the model as input:
    ``prompt_token_count`` leaves them out, ``total_token_count`` counts them.
    """
    prompt = getattr(usage_metadata, "prompt_token_count", 0) or 0
    tool_use = getattr(usage_metadata, "tool_use_prompt_token_count", 0) or 0
    return prompt + tool_use


def _gemini_completion_tokens(usage_metadata: object) -> int:
    """Output tokens Google bills: the answer plus the thinking tokens.

    ``candidates_token_count`` leaves ``thoughts_token_count`` out, and thinking
    is billed at the output rate, so without it a thinking model's reasoning
    reached neither the run budget nor the traces.
    """
    answer = getattr(usage_metadata, "candidates_token_count", 0) or 0
    thoughts = getattr(usage_metadata, "thoughts_token_count", 0) or 0
    return answer + thoughts


def _gemini_usage(usage_metadata: object) -> dict:
    """The usage dict of one response, for ``chat()`` and both streams alike."""
    return {
        "prompt_tokens": _gemini_prompt_tokens(usage_metadata),
        "completion_tokens": _gemini_completion_tokens(usage_metadata),
        **_gemini_cached_prompt_details(usage_metadata),
    }


def _usage_from_gemini_metadata(usage_metadata: object) -> dict:
    """Normalize a Gemini ``usage_metadata`` object to the standard usage dict.

    Mirrors the non-streaming ``chat()`` usage shape so streamed turns are
    costed identically in Langfuse.  Returns ``{}`` when no metadata is
    present (or it carries no tokens) so "no usage" stays falsy downstream.
    """
    if not usage_metadata:
        return {}
    usage = _gemini_usage(usage_metadata)
    if not (usage["prompt_tokens"] or usage["completion_tokens"]):
        return {}
    return usage


def _gemini_cached_prompt_details(usage_metadata: object) -> dict:
    """The cached part of the prompt, in OpenAI's shape.

    ``prompt_token_count`` INCLUDES ``cached_content_token_count`` and stays
    inclusive (proxy clients see the same ``prompt_tokens``); the cached part
    rides as ``prompt_tokens_details.cached_tokens`` so the pricing bills it at
    the cache-read rate instead of the full input rate.
    """
    cached = getattr(usage_metadata, "cached_content_token_count", None)
    if isinstance(cached, int) and not isinstance(cached, bool) and cached > 0:
        return {"prompt_tokens_details": {"cached_tokens": cached}}
    return {}


def _finish_reason_is_max_tokens(finish_reason: object) -> bool:
    """Whether a candidate's ``finish_reason`` is the ``max_output_tokens`` cut.

    The SDK hands back a ``types.FinishReason`` member, a ``(str, Enum)`` whose
    ``str()`` is ``"FinishReason.MAX_TOKENS"`` - so the member is compared by
    name and by value (a member equals its value), never through ``str()``.
    The raw string and the protobuf number of older payloads also count.
    """
    if finish_reason is None:
        return False
    if getattr(finish_reason, "name", None) == "MAX_TOKENS":
        return True
    return finish_reason in ("MAX_TOKENS", 2, "2")


def _is_retryable_gemini(exc: Exception) -> bool:
    """Return True for transient Google/Gemini errors worth retrying.

    The ``google-genai`` SDK raises various error types.  We check for
    HTTP status codes (429, 500, 502, 503, 529) via common attributes
    and fall back to keyword matching in the error message for safety.
    """
    # Check for status_code attribute (google.api_core.exceptions style)
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and status in (429, 500, 502, 503, 529):
        return True
    # Keyword match on the exception string for resilience
    exc_lower = str(exc).lower()
    if any(
        kw in exc_lower
        for kw in (
            "overloaded",
            "rate limit",
            "resource exhausted",
            "quota",
            "temporarily unavailable",
            "503",
            "529",
        )
    ):
        return True
    # Network-level errors (ConnectionError, TimeoutError, etc.)
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


_GEMINI_FAMILY_RE = re.compile(r"^gemini-(\d+)(?:\.(\d+))?")


def _supports_thinking(model: str) -> bool:
    """Return True for the Gemini families that think: 2.5 and every later one."""
    match = _GEMINI_FAMILY_RE.match(model.lower())
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2) or 0)) >= (2, 5)


def _client_error_detail(exc: genai_errors.ClientError) -> str:
    """Concise, actionable reason from a Gemini ``ClientError`` for logs.

    The SDK error carries the API ``status`` (e.g. ``INVALID_ARGUMENT``) and a
    human-readable ``message`` (e.g. "API key not valid. Please pass a valid API
    key."). Surfacing them turns an opaque "returned 400" into an actionable log
    line so a misconfigured key (invalid / expired / API-restricted) is obvious
    instead of looking like a transient endpoint glitch.
    """
    status = getattr(exc, "status", None) or "UNKNOWN"
    message = getattr(exc, "message", None)
    return f"{status}: {message}" if message else str(status)


#: The ``ChatMessage._provider_extra`` key a model turn's raw parts are kept
#: under. The provider replays them verbatim, in place of the message content,
#: on every later request of the run: function calling needs the thought
#: signatures back where they were received (a Gemini 3 call without its
#: signature is a 400), and they are what the next request is weighed on.
GEMINI_PARTS_KEY = "gemini_parts"


def _thought_signature_chars(signature: object) -> int:
    """The wire size of a thought signature: the base64 of its bytes.

    Opaque to the platform, and billed as input when it is sent back ("thought
    signatures will increase the input tokens you are charged", Google's
    thinking docs), so it weighs what the request carries.
    """
    if isinstance(signature, (bytes, bytearray)):
        return 4 * ((len(signature) + 2) // 3)
    if isinstance(signature, str):
        return len(signature)
    return 0


def _json_chars(value: object) -> int:
    """Characters of *value* serialized as JSON: how a structured field (the
    arguments of a function call, the payload of a function response) is sent."""
    if value is None:
        return 0
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(value))


def measure_gemini_parts(parts: Iterable[object]) -> tuple[int, int, int]:
    """``(text_chars, image_count, native_file_bytes)`` of Gemini content parts.

    The ONE Gemini counter: the provider's overflow check runs it over the
    whole payload (:meth:`GeminiProvider._measure_gemini_payload`) and the
    agent loop over the parts a model turn replays (the provider extras
    counter below), so the two can never disagree. A text part counts
    its characters, a thought part included; a function call its name and JSON
    arguments and a function response its name and JSON payload, as
    ``_measure_oai_payload`` counts tool calls; a thought signature, on
    whichever part it rides, its wire size.
    """
    text_chars = 0
    image_count = 0
    native_file_bytes = 0
    for part in parts or ():
        text_chars += _thought_signature_chars(getattr(part, "thought_signature", None))
        text = getattr(part, "text", None)
        if text:
            text_chars += len(text)
        elif (call := getattr(part, "function_call", None)) is not None:
            text_chars += len(getattr(call, "name", None) or "") + _json_chars(getattr(call, "args", None))
        elif (reply := getattr(part, "function_response", None)) is not None:
            text_chars += len(getattr(reply, "name", None) or "") + _json_chars(getattr(reply, "response", None))
        elif getattr(part, "inline_data", None):
            mime = getattr(part.inline_data, "mime_type", "") or ""
            data = getattr(part.inline_data, "data", b"")
            if mime.startswith("image/"):
                image_count += 1
            else:
                native_file_bytes += len(data) if isinstance(data, bytes) else 0
        elif getattr(part, "file_data", None):
            # Remote file URI — only created for images currently
            image_count += 1
    return text_chars, image_count, native_file_bytes


#: Where :func:`_gemini_parts_extra_chars` keeps the measure of the parts a
#: message replays: ``(parts, their elements, text_chars)``, so every later
#: context-gate pass reuses it instead of serializing each function call's
#: arguments again. The parts and their elements are held, and compared by
#: identity, so any change to the list re-measures it.
_PARTS_MEASURE_ATTR = "_gemini_parts_measure"


def _replayed_parts_text_chars(message: ChatMessage, parts: list | tuple) -> int:
    memo = getattr(message, _PARTS_MEASURE_ATTR, None)
    if (
        memo is not None
        and memo[0] is parts
        and len(memo[1]) == len(parts)
        and all(kept is part for kept, part in zip(memo[1], parts, strict=True))
    ):
        return memo[2]
    text_chars, _images, _native_bytes = measure_gemini_parts(parts)
    with contextlib.suppress(AttributeError):
        setattr(message, _PARTS_MEASURE_ATTR, (parts, tuple(parts), text_chars))
    return text_chars


def _gemini_parts_extra_chars(message: ChatMessage, parts: object) -> int:
    """What a model turn's replayed parts add to ``message.content``.

    The parts are sent IN PLACE of the content (see
    :meth:`GeminiProvider._build_gemini_contents`), so the message weighs
    whichever is larger; an empty list is not replayed and adds nothing. Read
    off the live message on every call: a gate that truncates the content in
    place frees nothing it did not free on the wire, and one that drops the
    message drops its parts. The parts' own measure does not depend on the
    content, so it is taken once per parts list (:data:`_PARTS_MEASURE_ATTR`).
    """
    if not isinstance(parts, (list, tuple)) or not parts:
        return 0
    return _replayed_parts_text_chars(message, parts) - len(message.content or "")


register_provider_extra_counter(GEMINI_PARTS_KEY, _gemini_parts_extra_chars)


class GeminiProvider(BaseLLMProvider):
    """Wraps the Google Gemini SDK for chat completions with tool calling."""

    provider_name = "gemini"
    # Default chat model used by chat / chat_stream / chat_stream_with_tools
    # when no explicit model is provided.  Exposed as a class attribute so
    # the FailoverLLMProvider can resolve per-model max_output_tokens
    # overrides for fallback calls that arrive with model=None.
    default_model = "gemini-3.8-flash"
    context_window = 1_000_000
    max_output_tokens = 65_536
    # Gemini has no documented hard tool-count limit and supports large
    # context windows.  Use a generous cap.
    max_tools = 512

    # ``generate_content_stream`` accepts the same ``GenerateContentConfig``
    # as the non-streaming path, so ``response_mime_type`` /
    # ``response_schema`` enforce the JSON schema while streaming too.
    supports_streaming_structured_output: bool = True

    # Reasoning-effort mapping is MODEL-AWARE:
    #
    # * Gemini 2.5 models take a token ``thinking_budget``.
    # * Gemini 3.x models replaced budgets with discrete thinking LEVELS
    #   (``low`` / ``medium`` / ``high``; ``minimal`` was removed in 3.8 and
    #   returns HTTP 400) and REJECT ``thinking_budget`` — sending a budget
    #   to a Gemini 3 model is unsupported per Google's migration guidance.
    #
    # ``xhigh`` / ``max`` (Anthropic / OpenAI top tiers) collapse to the
    # highest supported level or budget; ``minimal`` maps to ``low`` per
    # Google's own 3.7 -> 3.8 migration mapping.
    _EFFORT_BUDGETS = {"low": 1024, "medium": 4096, "high": 8192, "max": 16384}
    _EFFORT_LEVELS = {
        "minimal": types.ThinkingLevel.LOW,
        "low": types.ThinkingLevel.LOW,
        "medium": types.ThinkingLevel.MEDIUM,
        "high": types.ThinkingLevel.HIGH,
        "xhigh": types.ThinkingLevel.HIGH,
        "max": types.ThinkingLevel.HIGH,
    }

    def _build_thinking_config(
        self, model: str, reasoning_effort: str,
    ) -> types.ThinkingConfig:
        """Map a platform reasoning_effort onto the model's thinking dialect."""
        if model.startswith("gemini-3"):
            level = self._EFFORT_LEVELS.get(reasoning_effort, types.ThinkingLevel.HIGH)
            return types.ThinkingConfig(thinking_level=level)
        budget = self._EFFORT_BUDGETS.get(reasoning_effort, 8192)
        return types.ThinkingConfig(thinking_budget=budget)

    def _streaming_thinking_config(
        self, model: str, kwargs: dict,
    ) -> types.ThinkingConfig | None:
        """Thinking config of a streaming call: the reasoning-effort dialect,
        plus thought summaries when an ``on_thinking`` consumer reads them.

        Gemini returns no thought text at all without ``include_thoughts``, so
        the chat reasoning window stayed empty on every Gemini model.
        """
        config = None
        if reasoning_effort := kwargs.get("reasoning_effort"):
            config = self._build_thinking_config(model, reasoning_effort)
        if reads_reasoning(kwargs.get("on_thinking")) and _supports_thinking(model):
            config = config or types.ThinkingConfig()
            config.include_thoughts = True
        return config

    def __init__(self, api_key: str):
        # The speech routes the SDK in use does not wrap are called with it.
        self._api_key = api_key
        self.client = genai.Client(
            api_key=api_key,
            http_options={"timeout": 120_000},
        )

    # ------------------------------------------------------------------
    # Message building (shared by chat, chat_stream_with_tools)
    # ------------------------------------------------------------------

    async def _prepare_request(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None,
        model: str,
    ) -> tuple[str | None, list[types.Content]]:
        """Resolve attachments, build the payload and run the overflow check.

        The one path for ``chat``, ``chat_stream`` and
        ``chat_stream_with_tools``: attachments resolved together
        under Gemini's page cap and a share of *model*'s context window, the
        native PDFs costed by their pages in the overflow check.
        """
        plan = await resolve_payload_files(
            messages,
            "gemini",
            context_window=self._resolve_context_window(model),
        )
        system_instruction, contents = await self._build_gemini_contents(
            messages, file_plan=plan
        )
        text_chars, image_count, native_file_bytes = self._measure_gemini_payload(
            system_instruction, contents
        )
        self._check_resolved_overflow(
            text_chars=text_chars,
            image_count=image_count,
            native_file_bytes=max(native_file_bytes - plan.native_pdf_bytes, 0),
            native_document_tokens=plan.native_pdf_tokens,
            tools=tools,
            model=model,
        )
        return system_instruction, contents

    async def _build_gemini_contents(
        self,
        messages: list[ChatMessage],
        *,
        file_plan: PayloadFilePlan | None = None,
    ) -> tuple[str | None, list[types.Content]]:
        """Convert generic ChatMessages to Gemini-native format.

        Returns ``(system_instruction, contents)``. *file_plan* is the
        request-wide attachment resolution (:func:`resolve_payload_files`),
        built here when the caller has none.
        """
        if file_plan is None:
            file_plan = await resolve_payload_files(messages, "gemini")
        system_instruction: str | None = None
        contents: list[types.Content] = []

        for m_idx, msg in enumerate(tool_results_in_call_order(messages)):
            if msg.role == "system":
                system_instruction = (
                    (system_instruction + "\n\n" + msg.content)
                    if system_instruction
                    else msg.content
                )

            elif msg.role == "user":
                user_parts: list[types.Part] = [types.Part.from_text(text=msg.content)]
                if msg.images:
                    import base64

                    for i, img in enumerate(msg.images):
                        if img.startswith(("http://", "https://")):
                            user_parts.append(
                                types.Part.from_uri(
                                    file_uri=img,
                                    mime_type="image/jpeg",
                                )
                            )
                        else:
                            prepared = await prepare_base64_image_for_llm_async(
                                img,
                                source=f"gemini:user_message_image:{i}",
                            )
                            if prepared is None:
                                user_parts.append(
                                    types.Part.from_text(text=IMAGE_UNAVAILABLE_TEXT)
                                )
                                continue
                            optimized, mime = prepared
                            user_parts.append(
                                types.Part.from_bytes(
                                    data=base64.b64decode(optimized),
                                    mime_type=mime,
                                )
                            )
                if msg.files:
                    for f_idx, file_ref in enumerate(msg.files):
                        resolved = file_plan.get(m_idx, f_idx)
                        if resolved is None:
                            resolved = await resolve_file(file_ref, "gemini")
                        if isinstance(resolved, NativeFile):
                            if resolved.content_type.startswith("image/"):
                                prepared_bytes = await prepare_image_bytes_for_llm_async(
                                    resolved.data,
                                    resolved.content_type,
                                    source=(
                                        f"gemini:user_file_image:{f_idx}"
                                        f"[{file_ref.file_id}]"
                                    ),
                                )
                                if prepared_bytes is None:
                                    user_parts.append(
                                        types.Part.from_text(text=IMAGE_UNAVAILABLE_TEXT)
                                    )
                                    continue
                                opt_bytes, opt_mime = prepared_bytes
                                user_parts.append(
                                    types.Part.from_bytes(
                                        data=opt_bytes,
                                        mime_type=opt_mime,
                                    )
                                )
                            else:
                                user_parts.append(
                                    types.Part.from_bytes(
                                        data=resolved.data,
                                        mime_type=resolved.content_type,
                                    )
                                )
                        elif isinstance(resolved, FallbackText):
                            user_parts.append(types.Part.from_text(text=resolved.text))
                contents.append(types.Content(role="user", parts=user_parts))

            elif msg.role == "assistant":
                extra = getattr(msg, "_provider_extra", None) or {}
                gemini_parts = extra.get(GEMINI_PARTS_KEY)
                if gemini_parts:
                    contents.append(types.Content(role="model", parts=gemini_parts))
                else:
                    parts: list[types.Part] = []
                    if msg.content:
                        parts.append(types.Part.from_text(text=msg.content))
                    if msg.tool_calls:
                        tc_lines = []
                        for tc in msg.tool_calls:
                            tc_lines.append(
                                f"[Called tool `{tc.name}` with: "
                                f"{json.dumps(tc.arguments, default=str)}]"
                            )
                        parts.append(types.Part.from_text(text="\n".join(tc_lines)))
                    contents.append(types.Content(role="model", parts=parts))

            elif msg.role == "tool":
                result_text = msg.content
                extra_parts: list[types.Part] = []
                if msg.files:
                    images, extra_texts = await prepare_tool_result_attachments(
                        msg.files,
                        "gemini",
                        source=f"gemini:tool_result:{msg.tool_call_id}",
                    )
                    if extra_texts:
                        result_text = (
                            (result_text + "\n\n" if result_text else "")
                            + "\n\n".join(extra_texts)
                        )
                    for img in images:
                        extra_parts.append(
                            types.Part.from_bytes(
                                data=img.data,
                                mime_type=img.mime,
                            )
                        )
                    if images and TOOL_RESULT_IMAGE_DATA_NOTE:
                        result_text = (
                            (result_text + "\n\n" if result_text else "")
                            + TOOL_RESULT_IMAGE_DATA_NOTE
                        )
                fn_response = types.Part.from_function_response(
                    name=msg.name or "unknown",
                    response={"result": result_text},
                )
                turn_parts = [fn_response, *extra_parts]
                if (
                    contents
                    and contents[-1].role == "user"
                    and contents[-1].parts
                    and any(
                        hasattr(p, "function_response") and p.function_response
                        for p in contents[-1].parts
                    )
                ):
                    contents[-1].parts.extend(turn_parts)
                else:
                    contents.append(
                        types.Content(role="user", parts=turn_parts)
                    )

        return system_instruction, contents

    @staticmethod
    def _measure_gemini_payload(
        system_instruction: str | None,
        contents: list[types.Content],
    ) -> tuple[int, int, int]:
        """Measure a Gemini-format payload after file resolution.

        Returns (text_chars, image_count, native_file_bytes), each content's
        parts counted by :func:`measure_gemini_parts` - the counter the agent
        loop weighs a replayed model turn with.
        """
        text_chars = len(system_instruction or "")
        image_count = 0
        native_file_bytes = 0

        for content in contents:
            chars, images, native_bytes = measure_gemini_parts(content.parts or [])
            text_chars += chars
            image_count += images
            native_file_bytes += native_bytes

        return text_chars, image_count, native_file_bytes

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

        system_instruction, contents = await self._prepare_request(
            messages, tools, model
        )

        # --- build config ---
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )

        if system_instruction:
            config.system_instruction = system_instruction

        # Reasoning effort → thinking level (3.x) or thinking budget (2.5)
        if reasoning_effort := kwargs.get("reasoning_effort"):
            config.thinking_config = self._build_thinking_config(model, reasoning_effort)

        # Structured output: native Gemini JSON schema enforcement
        structured_output_cfg = kwargs.get("structured_output")
        if isinstance(structured_output_cfg, dict):
            config.response_mime_type = structured_output_cfg.get(
                "response_mime_type", "application/json"
            )
            if schema := structured_output_cfg.get("response_schema"):
                config.response_schema = schema

        # --- tools ---
        if tools:
            config.tools = [_gemini_tool(tools)]

        # --- call (with retry on transient errors) ---
        response = await retry_llm_call(
            lambda: self.client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            ),
            is_retryable=_is_retryable_gemini,
        )

        # --- parse response ---
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_parts: list[types.Part] = []

        if (
            response.candidates
            and response.candidates[0].content
            and response.candidates[0].content.parts
        ):
            raw_parts = list(response.candidates[0].content.parts)
            for part in raw_parts:
                if part.text and getattr(part, "thought", False):
                    thinking_parts.append(part.text)
                elif part.text:
                    text_parts.append(part.text)
                elif part.function_call:
                    fc = part.function_call
                    tool_calls.append(
                        ToolCall(
                            id=f"call_{fc.name}_{len(tool_calls)}",
                            name=fc.name,
                            arguments=dict(fc.args) if fc.args else {},
                        )
                    )

        # Determine stop reason — read the native finish_reason from the API
        # before falling back to tool-call inference.
        _raw_finish = None
        if response.candidates:
            _raw_finish = getattr(response.candidates[0], "finish_reason", None)
        if tool_calls:
            stop_reason = "tool_use"
        elif _finish_reason_is_max_tokens(_raw_finish):
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"

        # Usage
        usage = {}
        if response.usage_metadata:
            usage = _gemini_usage(response.usage_metadata)

        provider_extra = None
        if (tool_calls or thinking_parts) and raw_parts:
            provider_extra = {GEMINI_PARTS_KEY: raw_parts}

        return ChatResponse(
            content="\n".join(text_parts),
            model=model,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            stop_reason=stop_reason,
            thinking="\n".join(thinking_parts) if thinking_parts else None,
            _provider_extra=provider_extra,
        )

    # ------------------------------------------------------------------
    # chat_stream (native Gemini streaming)
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
        """Stream text chunks from Gemini."""
        model = model or self.default_model
        usage_sink = usage_sink_of(kwargs)
        if usage_sink is not None:
            usage_sink.restart()

        system_instruction, contents = await self._prepare_request(
            messages, tools, model
        )

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        if system_instruction:
            config.system_instruction = system_instruction

        # Reasoning effort → thinking level (3.x) or thinking budget (2.5)
        if thinking_config := self._streaming_thinking_config(model, kwargs):
            config.thinking_config = thinking_config

        # Structured output: native Gemini JSON schema enforcement —
        # ``generate_content_stream`` honours the same config fields as
        # the non-streaming ``generate_content`` path.
        structured_output_cfg = kwargs.get("structured_output")
        if isinstance(structured_output_cfg, dict):
            config.response_mime_type = structured_output_cfg.get(
                "response_mime_type", "application/json"
            )
            if schema := structured_output_cfg.get("response_schema"):
                config.response_schema = schema

        self.last_stop_reason = None
        self.last_usage = None
        # Retry transient errors on stream establishment only — nothing has
        # been yielded to the caller until the stream call returns.
        stream = await retry_llm_call(
            lambda: self.client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            ),
            is_retryable=_is_retryable_gemini,
        )
        on_thinking = kwargs.get("on_thinking")

        _usage_meta = None
        async for chunk in stream:
            if getattr(chunk, "usage_metadata", None):
                _usage_meta = chunk.usage_metadata
                if usage_sink is not None:
                    usage_sink.report(_usage_from_gemini_metadata(_usage_meta))
            if chunk.candidates:
                _fr = getattr(chunk.candidates[0], "finish_reason", None)
                if _finish_reason_is_max_tokens(_fr):
                    self.last_stop_reason = "max_tokens"
                elif _fr is not None:
                    self.last_stop_reason = "end_turn"
                if chunk.candidates[0].content:
                    for part in (chunk.candidates[0].content.parts or []):
                        if getattr(part, "thought", False) and part.text:
                            if on_thinking:
                                await on_thinking(part.text)
                        elif part.text:
                            yield part.text
            elif chunk.text:
                yield chunk.text
        self.last_usage = _usage_from_gemini_metadata(_usage_meta)

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

        system_instruction, contents = await self._prepare_request(
            messages, tools, model
        )

        # --- build config ---
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        if system_instruction:
            config.system_instruction = system_instruction
        if thinking_config := self._streaming_thinking_config(model, kwargs):
            config.thinking_config = thinking_config
        if tools:
            config.tools = [_gemini_tool(tools)]

        on_thinking = kwargs.get("on_thinking")

        # Retry is only safe while no output has escaped to the caller.  The
        # flag flips just before the first ``on_token`` / ``on_thinking``
        # delivery; afterwards the retryable predicate returns False and any
        # error propagates immediately (replaying would duplicate output).
        emitted = False

        async def _stream_once() -> ChatResponse:
            nonlocal emitted

            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            raw_parts: list[types.Part] = []
            has_tool_calls = False
            stop_reason = "end_turn"
            usage_meta = None
            if usage_sink is not None:
                usage_sink.restart()

            stream = await self.client.aio.models.generate_content_stream(
                model=model, contents=contents, config=config,
            )

            async for chunk in stream:
                # ``usage_metadata`` is cumulative; the final chunk carries the
                # complete counts.  Keep the last non-None value so the response
                # reports real token usage instead of an empty dict.
                if getattr(chunk, "usage_metadata", None):
                    usage_meta = chunk.usage_metadata
                    if usage_sink is not None:
                        usage_sink.report(_usage_from_gemini_metadata(usage_meta))
                if chunk.candidates:
                    _fr = getattr(chunk.candidates[0], "finish_reason", None)
                    if _finish_reason_is_max_tokens(_fr):
                        stop_reason = "max_tokens"
                    if chunk.candidates[0].content:
                        for part in (chunk.candidates[0].content.parts or []):
                            raw_parts.append(part)
                            if getattr(part, "thought", False) and part.text:
                                thinking_parts.append(part.text)
                                if on_thinking:
                                    emitted = True
                                    await on_thinking(part.text)
                            elif part.text:
                                text_parts.append(part.text)
                                if not has_tool_calls and on_token:
                                    emitted = True
                                    await on_token(part.text)
                            elif part.function_call:
                                has_tool_calls = True
                                fc = part.function_call
                                tool_calls.append(
                                    ToolCall(
                                        id=f"call_{fc.name}_{len(tool_calls)}",
                                        name=fc.name,
                                        arguments=dict(fc.args) if fc.args else {},
                                    )
                                )

            if tool_calls:
                stop_reason = "tool_use"
            elif stop_reason != "max_tokens":
                stop_reason = "end_turn"

            provider_extra = None
            if (tool_calls or thinking_parts) and raw_parts:
                provider_extra = {GEMINI_PARTS_KEY: raw_parts}

            return ChatResponse(
                content="".join(text_parts),
                model=model,
                usage=_usage_from_gemini_metadata(usage_meta),
                tool_calls=tool_calls if tool_calls else None,
                stop_reason=stop_reason,
                thinking="".join(thinking_parts) if thinking_parts else None,
                _provider_extra=provider_extra,
            )

        return await retry_llm_call(
            _stream_once,
            is_retryable=lambda exc: not emitted and _is_retryable_gemini(exc),
        )

    # ------------------------------------------------------------------
    # embed
    # ------------------------------------------------------------------

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """Generate embeddings using Gemini's embedding model.

        Returns the native vector produced by the model (768 dims for
        text-embedding-004).  Configure Vector Dimensions in Settings to
        match.
        """
        from app.llm.router import EMBEDDING_DEFAULTS

        model = model or EMBEDDING_DEFAULTS.get("gemini", "text-embedding-004")
        response = await self.client.aio.models.embed_content(
            model=model,
            contents=text,
        )
        return list(response.embeddings[0].values)

    # ------------------------------------------------------------------
    # Model listing
    # ------------------------------------------------------------------

    async def _list_all_model_ids(self) -> list[str]:
        """Return every model id exposed by the API, its ``models/`` (Gemini API) or
        ``publishers/google/models/`` (Vertex AI) prefix stripped.

        Two non-obvious details are handled here once so all three public
        listers stay correct and in sync:

        1. ``config={"query_base": True}`` explicitly requests *base* models.
           Older ``google-genai`` releases defaulted ``models.list()`` to
           *tuned* models (``GET /v1beta/tunedModels``), which returns HTTP 400
           on the Gemini Developer API -- the source of the recurring
           "endpoint returned 400" warnings. Newer releases already default to
           base models, so passing it is a harmless, version-proof no-op there.
        2. ``async for`` -- the SDK returns an ``AsyncPager`` that only supports
           asynchronous iteration (a plain ``for`` raises ``TypeError``) and it
           transparently fetches every page.
        """
        models: list[str] = []
        async for model in await self.client.aio.models.list(config={"query_base": True}):
            models.append((model.name or "").removeprefix("publishers/google/").removeprefix("models/"))
        return models

    async def _capability_listing(
        self, capability: str, keep: Callable[[str], bool], built_in: tuple[str, ...]
    ) -> list[str]:
        """The listed ids *keep* accepts, newest first; *built_in* only when the listing cannot be read.

        A listing that answers is what the key may use, even with none of
        *capability*'s models in it: the built-in ones would then offer models
        Gemini no longer serves.
        """
        try:
            ids = await self._list_all_model_ids()
        except genai_errors.ClientError as exc:
            if exc.code in (401, 403):
                raise
            logger.warning(
                "Gemini list_%s_models endpoint returned %s (%s) - returning the built-in list (may be stale)",
                capability,
                exc.code,
                _client_error_detail(exc),
            )
            return list(built_in)
        except Exception as exc:
            logger.warning(
                "Gemini list_%s_models failed - returning the built-in list (may be stale): %s",
                capability,
                format_exception_message(exc),
            )
            return list(built_in)
        return sorted({m for m in ids if keep(m)}, reverse=True)

    async def list_model_context_windows(self) -> dict[str, int] | None:
        """``input_token_limit`` of each base model, keyed like ``list_models``."""
        windows: dict[str, int] = {}
        async for model in await self.client.aio.models.list(config={"query_base": True}):
            model_id = model.name or ""
            if model_id.startswith("models/"):
                model_id = model_id[7:]
            tokens = reported_context_window(getattr(model, "input_token_limit", None))
            if model_id and tokens:
                windows[model_id] = tokens
        return windows

    async def list_models(self) -> list[str]:
        """The Gemini chat models: the image, speech and embedding ones are listed by their own capability."""
        from app.llm.router import is_chat_capable_model

        models = [
            m for m in await self._list_all_model_ids() if m.startswith("gemini-") and is_chat_capable_model(m)
        ]
        models.sort(reverse=True)
        return models

    async def list_embedding_models(self) -> list[str]:
        """The Gemini embedding models listed for the key."""
        return await self._capability_listing("embedding", lambda m: "embedding" in m, ("text-embedding-004",))

    async def list_image_models(self) -> list[str]:
        """The Gemini image models: Imagen, and the Gemini models that draw ("Nano Banana", ``*-image``)."""
        from app.llm.router import is_image_model

        return await self._capability_listing(
            "image",
            lambda m: m.startswith("imagen-") or (m.startswith("gemini-") and is_image_model(m)),
            ("imagen-3.0-generate-002",),
        )

    # ------------------------------------------------------------------
    # generate_image
    # ------------------------------------------------------------------

    async def generate_image(
        self,
        prompt: str,
        model: str | None = None,
        size: str = "1024x1024",
    ) -> bytes:
        """Generate an image and return its raw bytes.

        Imagen through its own API; a Gemini image model ("Nano Banana",
        ``gemini-*-image``) through ``generate_content``, asked for an image
        at the aspect ratio of *size*.
        """
        model = model or "imagen-3.0-generate-002"
        if not model.startswith("gemini-"):
            response = await self.client.aio.models.generate_images(
                model=model,
                prompt=prompt,
                config=types.GenerateImagesConfig(number_of_images=1),
            )
            return response.generated_images[0].image.image_bytes
        response = await self.client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=_aspect_ratio_of(size)),
            ),
        )
        for candidate in response.candidates or []:
            for part in getattr(candidate.content, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    return inline.data
        reason = getattr((response.candidates or [None])[0], "finish_reason", None) if response.candidates else None
        raise RuntimeError(f"{model} returned no image{f' ({reason})' if reason else ''}.")

    #: The reference pictures a Gemini image model is asked with.
    _IMAGE_REFERENCES = 3

    def image_reference_limit(self, model: str | None) -> int:
        """A Gemini image model draws from pictures given beside the prompt; Imagen through this API does not."""
        return self._IMAGE_REFERENCES if (model or "").startswith("gemini-") else 0

    async def edit_image(
        self,
        prompt: str,
        images: list[tuple[bytes, str]],
        *,
        mask: tuple[bytes, str] | None = None,
        model: str | None = None,
        size: str = "1024x1024",
    ) -> bytes:
        """An image from a Gemini image model given the pictures as parts before the prompt (no mask)."""
        if mask is not None:
            raise NotImplementedError(f"{model} takes no edit mask.")
        name = model or "gemini-2.5-flash-image"
        parts = [types.Part.from_bytes(data=data, mime_type=mime or "image/png") for data, mime in images]
        response = await self.client.aio.models.generate_content(
            model=name,
            contents=[*parts, prompt],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=_aspect_ratio_of(size)),
            ),
        )
        for candidate in response.candidates or []:
            for part in getattr(candidate.content, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    return inline.data
        reason = getattr((response.candidates or [None])[0], "finish_reason", None) if response.candidates else None
        raise RuntimeError(f"{name} returned no image{f' ({reason})' if reason else ''}.")

    # ------------------------------------------------------------------
    # video (Veo; Gemini Omni)
    # ------------------------------------------------------------------

    #: Veo is served by Vertex AI alone: the Gemini API makes videos with Gemini Omni.
    default_video_model = "gemini-omni-1.1-flash"

    #: Offered when the model list cannot be read.
    _FALLBACK_VIDEO_MODELS = ("gemini-omni-1.1-flash",)
    #: The durations Veo makes.
    _VIDEO_DURATIONS = (4, 6, 8)
    #: The request fields the attempts relax, as a refusal names them.
    _VIDEO_RELAXABLE_FIELDS = ("resolution", "duration", "negative", "seconds")

    #: The reference images a Veo 3.1 model takes (``referenceImages``, asset type), never with a start image,
    #: and a Gemini Omni model is asked with beside its start frame.
    _VIDEO_REFERENCE_IMAGES = 3
    #: The one length Veo makes with reference images.
    _REFERENCE_SECONDS = 8
    #: Whether the API takes Veo's ``generate_audio`` (Vertex AI does; the Gemini API refuses the field).
    _VIDEO_AUDIO_FIELD = False

    @staticmethod
    def _interactions_video(model: str) -> bool:
        """A Gemini Omni model makes its videos through the Interactions API; Veo through ``generate_videos``."""
        return (model or "").lower().startswith("gemini-omni-")

    def max_reference_images(self, model: str) -> int:
        """Veo 3.1, Veo 3.1 Fast and Gemini Omni take up to three images; Veo Lite and the earlier Veo none."""
        name = (model or "").lower()
        if self._interactions_video(name):
            return self._VIDEO_REFERENCE_IMAGES
        return self._VIDEO_REFERENCE_IMAGES if name.startswith("veo-3.1") and "lite" not in name else 0

    def reference_images_with_start_frame(self, model: str) -> bool:
        """Gemini Omni takes both, each image declared in its prompt; Veo refuses ``referenceImages`` with ``image``."""
        return self._interactions_video(model)

    def takes_negative_prompt(self, model: str) -> bool:
        """Veo takes ``negative_prompt`` in its generation config; Gemini Omni has no such field."""
        return not self._interactions_video(model)

    def holds_locked_camera(self, model: str, *, start_frame: bool) -> bool | None:
        """Veo keeps a locked-off camera still from a start frame; from a prompt alone it drifts (0.61-0.70)."""
        return start_frame if (model or "").lower().startswith("veo-") else None

    @staticmethod
    def _video_attempts(resolution: str, seconds: int) -> list[tuple[str | None, int | None, bool]]:
        """``(resolution, seconds, negative prompt)`` asked in turn while the request is refused as invalid.

        Each model takes its own subset (a resolution only at some lengths, no
        resolution at all, no negative prompt): the settings asked first, then
        720p, then the longest length with the model's own resolution, then
        the model's own defaults.
        """
        attempts: list[tuple[str | None, int | None, bool]] = [(resolution, seconds, True)]
        if resolution != "720p":
            attempts.append(("720p", seconds, True))
        attempts.extend([(None, 8, True), (None, None, False)])
        return list(dict.fromkeys(attempts))

    async def list_video_models(self) -> list[str]:
        """The video models Gemini lists for the key: Gemini Omni, and Veo while it is served.

        Gemini Omni also answers in text (the catalog lists it as chat), so it
        stays in the chat listing as well.
        """
        return await self._capability_listing(
            "video", lambda m: m.startswith("veo-") or self._interactions_video(m), self._FALLBACK_VIDEO_MODELS
        )

    async def probe_video_model(self, model: str) -> None:
        """An HTTP 404 when Gemini no longer serves *model*: read from its listing, else its record.

        A model Gemini shut down leaves the listing but keeps a record that
        ``models.get`` still answers, while its generations answer 404:
        the record is read only when the listing cannot be.
        """
        from app.llm.video import VideoAPIError

        try:
            listed = await self._list_all_model_ids()
        except Exception:
            await self.client.aio.models.get(model=model)
            return
        if model not in listed:
            raise VideoAPIError(404, f"{model} is not among the models Gemini lists for this key")

    async def generate_video(self, request, *, progress=None, on_job=None, resume_job=None, on_state=None):
        """Generate a video: a Veo long-running operation, or a Gemini Omni interaction, polled until done.

        *on_job* is told the operation's name (the interaction's id) as soon
        as it exists; with *resume_job* (one an earlier run of the shot was
        told), that job is polled instead of a new one being started.
        *on_state* hears the job's state at every poll.
        """
        from app.llm.video import VideoGenerationError, VideoResult, normalised

        notes: list[str] = []
        request = normalised(request, notes)
        if self._interactions_video(request.model):
            return await self._omni_video(
                request, notes, progress=progress, on_job=on_job, resume_job=resume_job, on_state=on_state
            )
        if resume_job:
            operation = types.GenerateVideosOperation(name=resume_job)
            data = await self._await_video_operation(
                operation, request.model, progress, resumed=True, on_state=on_state
            )
            return VideoResult(data=data, job_id=str(resume_job), notes=notes)
        operation = await self._start_video_operation(request, notes)
        if not operation.name:
            raise VideoGenerationError("The video provider started no generation it can be asked about.")
        if on_job is not None:
            await on_job(str(operation.name))
        data = await self._await_video_operation(operation, request.model, progress, on_state=on_state)
        return VideoResult(data=data, job_id=str(operation.name), notes=notes)

    @staticmethod
    def _omni_input(request) -> list[dict]:
        """A Gemini Omni interaction's input: the start frame, the reference images, then the prompt declaring them.

        Omni reads each image's role from its prompt (``[# Sources
        <FIRST_FRAME>@Image1]``, ``[# References <IMAGE_REF_0>@Image2]``) and
        has no field for the length, the single shot, the sound or what to
        avoid: they are written into the prompt, the way its guide asks.
        """
        import base64

        images: list[tuple[bytes, str]] = []
        declared: list[str] = []
        uses: list[str] = []
        if request.reference_image:
            images.append((request.reference_image, request.reference_mime or "image/png"))
            declared.append("[# Sources <FIRST_FRAME>@Image1]")
            uses.append("Use Image1 as the starting frame.")
        references: list[str] = []
        for data, mime in request.reference_images:
            images.append((data, mime or "image/png"))
            references.append(f"<IMAGE_REF_{len(references)}>@Image{len(images)}")
        if references:
            declared.append(f"[# References {' '.join(references)}]")
            uses.append(
                "Use the given images as references for video generation. "
                "The images should not be used as literal initial frames."
            )
        directions = [
            " ".join([*declared, request.prompt.strip()]),
            f"In a single continuous shot, no scene cuts, {request.duration_seconds} seconds long.",
        ]
        if request.sound is False:
            directions.append("No audio: no dialogue, no music, no sound effects.")
        if request.negative_prompt:
            directions.append(f"Do not show: {request.negative_prompt.strip()}.")
        directions.extend(uses)
        blocks: list[dict] = [
            {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mime_type": mime} for data, mime in images
        ]
        blocks.append({"type": "text", "text": "\n".join(directions)})
        return blocks

    async def _omni_video(self, request, notes: list[str], *, progress, on_job, resume_job, on_state):
        """A Gemini Omni clip: a background interaction, polled until it ends, its video in its output.

        The interaction's id is the shot's job: a resumed shot polls it and
        never starts a second, paid one.
        """
        from urllib.parse import quote

        from app.llm.video import (
            VideoAPIError,
            VideoGenerationError,
            VideoProviderBusyError,
            VideoResult,
            await_finished_job,
            status_error,
        )

        headers = {"x-goog-api-key": self._api_key}
        if resume_job:
            interaction_id = str(resume_job)
        else:
            body = {
                "model": request.model,
                "input": self._omni_input(request),
                "response_format": {
                    "type": "video",
                    "aspect_ratio": request.aspect_ratio,
                    "resolution": request.resolution,
                },
                "background": True,
            }
            try:
                async with httpx.AsyncClient(timeout=120) as http:
                    response = await http.post(f"{self._API_ROOT}/interactions", json=body, headers=headers)
            except _VIDEO_TRANSPORT_ERRORS as exc:
                # Nothing was started: the job asks again after its back-off.
                raise VideoProviderBusyError(
                    f"The video provider could not be reached: {format_exception_message(exc)}"
                ) from exc
            if response.status_code >= 400:
                refused = VideoAPIError.of(response, _gemini_reason(response))
                raise status_error(refused.code, refused, request.model) from None
            interaction_id = str(response.json().get("id") or "")
            if not interaction_id:
                raise VideoGenerationError("The video provider started no generation it can be asked about.")
            notes.append(f"{request.model} has no length setting: {request.duration_seconds} s was asked in its prompt.")
            if on_job is not None:
                await on_job(interaction_id)

        async def read():
            try:
                async with httpx.AsyncClient(timeout=180) as http:
                    answer = await http.get(
                        f"{self._API_ROOT}/interactions/{quote(interaction_id, safe='')}", headers=headers
                    )
            except _VIDEO_TRANSPORT_ERRORS as exc:
                raise VideoAPIError(None, format_exception_message(exc)) from None
            if answer.status_code >= 400:
                raise VideoAPIError.of(answer, _gemini_reason(answer))
            return _omni_state(answer.json())

        state = await await_finished_job(read, request.model, progress, resumed=bool(resume_job), on_state=on_state)
        return VideoResult(data=state.data or b"", job_id=interaction_id, notes=notes)

    async def _start_video_operation(self, request, notes: list[str]):
        """The Veo operation of *request*, its settings relaxed in turn while the model refuses one."""
        from app.llm.video import (
            VideoGenerationError,
            VideoProviderBusyError,
            api_error_message,
            names_a_field,
            snap_duration,
            status_error,
        )

        seconds = snap_duration(request.duration_seconds, self._VIDEO_DURATIONS, notes)
        image = None
        if request.reference_image:
            image = types.Image(image_bytes=request.reference_image, mime_type=request.reference_mime or "image/png")
        references = [
            types.VideoGenerationReferenceImage(
                image=types.Image(image_bytes=data, mime_type=mime or "image/png"),
                reference_type=types.VideoGenerationReferenceType.ASSET,
            )
            for data, mime in request.reference_images
        ] or None
        if references and seconds != self._REFERENCE_SECONDS:
            notes.append(f"Veo makes a shot with reference images at {self._REFERENCE_SECONDS} s only: generated at that length.")
            seconds = self._REFERENCE_SECONDS
        operation = None
        refused: BaseException | None = None
        for resolution, length, negative in self._video_attempts(request.resolution, seconds):
            config = types.GenerateVideosConfig(
                aspect_ratio=request.aspect_ratio,
                number_of_videos=1,
                duration_seconds=length,
                resolution=resolution,
                negative_prompt=(request.negative_prompt or None) if negative else None,
                reference_images=references,
                generate_audio=(request.sound is not False) if self._VIDEO_AUDIO_FIELD else None,
            )
            try:
                operation = await self.client.aio.models.generate_videos(
                    model=request.model, prompt=request.prompt, image=image, config=config
                )
            except genai_errors.ClientError as exc:
                if exc.code == 400:
                    refused = exc
                    if not names_a_field(exc, self._VIDEO_RELAXABLE_FIELDS):
                        break
                    continue
                raise self._video_refusal(exc, request.model) from None
            except genai_errors.ServerError as exc:
                raise status_error(exc.code, exc, request.model) from None
            except _VIDEO_TRANSPORT_ERRORS as exc:
                # Nothing was started: the job asks again after its back-off.
                raise VideoProviderBusyError(
                    f"The video provider could not be reached: {format_exception_message(exc)}"
                ) from exc
            if (resolution, length) != (request.resolution, seconds):
                made = " ".join(
                    part
                    for part in (
                        f"at {resolution}" if resolution else "at the model's resolution",
                        f"for {length} s" if length else "for the model's length",
                    )
                )
                notes.append(f"The model refused {request.resolution} for {seconds} s: generated {made}.")
            break
        if operation is None:
            detail = api_error_message(refused) if refused is not None else "no settings accepted"
            raise VideoGenerationError(f"The video model refused the request: {detail}")
        return operation

    def _video_refusal(self, exc: genai_errors.ClientError, model: str) -> RuntimeError:
        """The error a Veo request refused with an HTTP 4xx raises; a provider may word its own refusals."""
        from app.llm.video import status_error

        return status_error(exc.code, exc, model)

    async def _await_video_operation(
        self, operation, model: str, progress, *, resumed: bool = False, on_state=None
    ) -> bytes:
        """Poll the operation that runs upstream until it ends, then download its video.

        A transient failure - an HTTP 429 / 5xx or no answer at all (a reset
        connection, a timeout the SDK does not wrap) - is asked again after a
        back-off within the transient budget: never turned into a retry that
        would make a second video. *on_state* hears ``running`` / ``done`` at
        every answer (a Veo operation reports no percent) and the failure
        being ridden out.
        """
        import asyncio
        import time

        from app.llm.provider_errors import rpc_busy_status, status_retry_delay, status_says_quota
        from app.llm.video import (
            MAX_WAIT_SECONDS,
            POLL_SECONDS,
            TransientFailures,
            VideoGenerationError,
            VideoJobState,
            VideoProviderBusyError,
        )

        def code_of(exc: BaseException) -> int | None:
            return getattr(exc, "code", None) if isinstance(exc, genai_errors.APIError) else None

        async def tell(state: str) -> None:
            if on_state is not None:
                await on_state(VideoJobState(state))

        started = time.monotonic()
        failures = TransientFailures(model)
        first = resumed
        if not resumed:
            await tell("running")
        while first or not operation.done:
            if time.monotonic() - started > MAX_WAIT_SECONDS:
                raise VideoGenerationError("The video model did not finish within 30 minutes.")
            if not first:
                await asyncio.sleep(POLL_SECONDS + failures.wait)
            try:
                operation = await self.client.aio.operations.get(operation)
            except genai_errors.ClientError as exc:
                if resumed and exc.code == 404:
                    raise VideoGenerationError(
                        "The provider no longer knows the generation this shot started: generate it again."
                    ) from None
                failures.failed(exc, code_of(exc))
                first = False
                await tell(failures.state)
                continue
            except (genai_errors.APIError, *_VIDEO_TRANSPORT_ERRORS) as exc:
                failures.failed(exc, code_of(exc))
                first = False
                await tell(failures.state)
                continue
            first = False
            failures.ok()
            if progress is not None:
                await progress(int(min(90, (time.monotonic() - started) / 2)))
            await tell("done" if operation.done else "running")
        if operation.error:
            detail = operation.error.get("message") if isinstance(operation.error, dict) else operation.error
            if (busy := rpc_busy_status(operation.error)) is not None:
                # The service's own state ended the operation (its load, its quota): nothing was made or paid,
                # and the shot is asked again once it can take it - never failed for it.
                raise VideoProviderBusyError(
                    f"The video provider could not make the video now (HTTP {busy}): {detail}",
                    status_code=busy,
                    retry_after=status_retry_delay(operation.error),
                    quota=status_says_quota(operation.error),
                    started=True,
                )
            raise VideoGenerationError(f"The video was not made: {detail}")
        videos = (operation.response.generated_videos if operation.response else None) or []
        if not videos or videos[0].video is None:
            raise VideoGenerationError("The model returned no video (the prompt may have been filtered).")
        video = videos[0].video
        if video.video_bytes:
            return bytes(video.video_bytes)
        return await self._download_video(video, model)

    async def _download_video(self, video, model: str) -> bytes:
        """A made video the operation names without its bytes, read through the Gemini API's Files API."""
        import asyncio

        from app.llm.video import DOWNLOAD_ATTEMPTS, TransientFailures, VideoGenerationError

        downloads = TransientFailures(model, budget=0, attempts=DOWNLOAD_ATTEMPTS)
        while True:
            try:
                data = await self.client.aio.files.download(file=video)
            except (genai_errors.APIError, *_VIDEO_TRANSPORT_ERRORS) as exc:
                downloads.failed(exc, exc.code if isinstance(exc, genai_errors.APIError) else None)
                await asyncio.sleep(downloads.wait)
                continue
            if not data:
                raise VideoGenerationError("The video provider returned an empty video.")
            return bytes(data)

    # ------------------------------------------------------------------
    # speech
    # ------------------------------------------------------------------

    default_speech_model = "gemini-3.8-flash-tts"

    #: Offered when the model list cannot be read: the current TTS models.
    _FALLBACK_SPEECH_MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")
    #: The prebuilt studio voices and how Gemini describes them. The extended
    #: library and the project's designed or replicated voices come from
    #: ``GET /v1beta/voices``.
    _PREBUILT_VOICES = (
        ("Kore", "Firm"),
        ("Zephyr", "Bright"),
        ("Puck", "Upbeat"),
        ("Charon", "Informative"),
        ("Fenrir", "Excitable"),
        ("Leda", "Youthful"),
        ("Orus", "Firm"),
        ("Aoede", "Breezy"),
        ("Callirrhoe", "Easy-going"),
        ("Autonoe", "Bright"),
        ("Enceladus", "Breathy"),
        ("Iapetus", "Clear"),
        ("Umbriel", "Easy-going"),
        ("Algieba", "Smooth"),
        ("Despina", "Smooth"),
        ("Erinome", "Clear"),
        ("Algenib", "Gravelly"),
        ("Rasalgethi", "Informative"),
        ("Laomedeia", "Upbeat"),
        ("Achernar", "Soft"),
        ("Alnilam", "Firm"),
        ("Schedar", "Even"),
        ("Gacrux", "Mature"),
        ("Pulcherrima", "Forward"),
        ("Achird", "Friendly"),
        ("Zubenelgenubi", "Casual"),
        ("Vindemiatrix", "Gentle"),
        ("Sadachbia", "Lively"),
        ("Sadaltager", "Knowledgeable"),
        ("Sulafat", "Warm"),
    )
    #: Ids of the designed and replicated voices, which a library read may not list.
    _CUSTOM_VOICE_PREFIXES = ("voice_", "voicekey_")
    _API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

    @staticmethod
    def _preview_tts(model: str) -> bool:
        """The preview TTS models (``gemini-2.5-pro-preview-tts``...) speak through ``generateContent`` and return PCM."""
        return "preview" in model.lower()

    async def list_speech_models(self) -> list[str]:
        """The Gemini text-to-speech models listed for the key."""
        return await self._capability_listing("speech", lambda m: "tts" in m, self._FALLBACK_SPEECH_MODELS)

    @classmethod
    def prebuilt_voices(cls) -> list:
        """The studio voices, Kore (the default) first."""
        from app.llm.speech import Voice

        return [Voice(name, name, "prebuilt", trait) for name, trait in cls._PREBUILT_VOICES]

    async def list_voices(self, model: str | None = None) -> list:
        """The studio voices first (Kore, the default), then the project's own voices and the extended library."""
        prebuilt = self.prebuilt_voices()
        if model and self._preview_tts(model):
            return prebuilt
        try:
            library = await self._voice_library()
        except Exception as exc:
            logger.info("Gemini voice library not read: %s", format_exception_message(exc))
            return prebuilt
        known = {v.id for v in prebuilt}
        custom = [v for v in library if v.category in ("prompted", "replicated")]
        extended = [v for v in library if v.id not in known and v not in custom]
        return prebuilt + custom + extended

    async def _voice_library(self) -> list:
        """The project's voice library, reused for a few minutes: every scene of a narration asks."""
        import time

        cached = getattr(self, "_voice_library_cache", None)
        if cached and time.monotonic() - cached[0] < 300:
            return list(cached[1])
        voices = await self._read_voice_library()
        self._voice_library_cache = (time.monotonic(), voices)
        return list(voices)

    async def _read_voice_library(self) -> list:
        import httpx

        from app.llm.speech import Voice

        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.get(
                f"{self._API_ROOT}/voices", params={"page_size": 1000}, headers={"x-goog-api-key": self._api_key}
            )
        _raise_for_speech(response)
        voices = []
        for item in response.json().get("voices") or []:
            if not isinstance(item, dict) or not (voice_id := item.get("id") or item.get("name")):
                continue
            traits = [
                item.get("language_code") or item.get("languageCode"),
                item.get("accent"),
                item.get("gender"),
                item.get("description"),
            ]
            voices.append(
                Voice(
                    str(voice_id),
                    str(item.get("display_name") or item.get("displayName") or voice_id),
                    str(item.get("type") or ""),
                    ", ".join(str(t) for t in traits if t),
                )
            )
        return voices

    def speech_takes_style(self, model: str) -> bool | None:
        """Every Gemini TTS model is directed in words: the style is a ``speech_metadata`` annotation or the prompt."""
        return True

    async def synthesize_speech(self, request):
        """Speak with a Gemini TTS model: the Interactions API, ``generateContent`` for the preview models."""
        import base64

        from app.llm.speech import SpeechResult, clamp_speed, pcm_to_wav, pick_voice

        notes: list[str] = []
        wanted = (request.voice or "").strip()
        prebuilt = {name.lower(): name for name, _trait in self._PREBUILT_VOICES}
        if wanted.startswith(self._CUSTOM_VOICE_PREFIXES):
            voice = wanted
        elif not wanted or wanted.lower() in prebuilt:
            voice = prebuilt.get(wanted.lower(), self._PREBUILT_VOICES[0][0])
        else:
            voice = pick_voice(await self.list_voices(request.model), wanted, self._PREBUILT_VOICES[0][0], notes)
        # The pace is a delivery direction, as Gemini documents it.
        speed = clamp_speed(request.speed)
        pace = "speaking slowly" if speed < 0.95 else "speaking rapidly" if speed > 1.05 else None
        style = ", ".join(s for s in ((request.style or "").strip(), pace) if s) or None
        if self._preview_tts(request.model):
            prompt = f"{style}:\n{request.text}" if style else request.text
            response = await self.client.aio.models.generate_content(
                model=request.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
                    ),
                ),
            )
            for candidate in response.candidates or []:
                for part in (candidate.content.parts if candidate.content else None) or []:
                    data = getattr(part, "inline_data", None)
                    if data is not None and data.data:
                        return SpeechResult(pcm_to_wav(data.data), "audio/wav", voice, notes)
            raise _speech_error("The speech model returned no audio.")

        import httpx

        text: dict = {"type": "text", "text": request.text}
        if style:
            text["annotations"] = [{"type": "speech_metadata", "style": style}]
        body = {
            "model": request.model,
            "input": [{"type": "user_input", "content": [text]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": voice}]},
        }
        async with httpx.AsyncClient(timeout=180) as http:
            response = await http.post(
                f"{self._API_ROOT}/interactions", json=body, headers={"x-goog-api-key": self._api_key}
            )
        _raise_for_speech(response)
        audio = [b for b in _interaction_outputs(response.json()) if b.get("type") == "audio" and b.get("data")]
        if not audio:
            raise _speech_error("The speech model returned no audio.")
        data = base64.b64decode(audio[-1]["data"])
        mime = str(audio[-1].get("mime_type") or audio[-1].get("mimeType") or "audio/wav")
        if "l16" in mime or "pcm" in mime:
            data = pcm_to_wav(data)
        return SpeechResult(data, "audio/wav", voice, notes)

    # ------------------------------------------------------------------
    # music
    # ------------------------------------------------------------------

    #: Lyria 3.5 composes whole pieces whose length the prompt sets; Lyria 3
    #: Clip always makes 30 seconds.
    default_music_model = "lyria-3.5"
    _FALLBACK_MUSIC_MODELS = ("lyria-3.5", "lyria-3-clip-preview")
    _CLIP_MUSIC_SECONDS = 30.0

    @staticmethod
    def _fixed_length_music(model: str) -> bool:
        return "clip" in model.lower()

    async def list_music_models(self) -> list[str]:
        """The Lyria models the Interactions API serves (the streaming RealTime model excluded)."""
        return await self._capability_listing(
            "music", lambda m: m.startswith("lyria") and "realtime" not in m, self._FALLBACK_MUSIC_MODELS
        )

    async def generate_music(self, request):
        """Compose with Lyria: one interaction answered with the audio (MP3, 44.1 kHz stereo) and its structure.

        Lyria takes its directions in words: the length (Lyria 3.5), no vocals
        and what to avoid are written into the prompt, the way its guide asks.
        """
        import base64

        import httpx

        from app.llm.music import MusicGenerationError, MusicResult, clamp_seconds, length_label, status_error

        notes: list[str] = []
        directions = [request.prompt.strip()]
        seconds = clamp_seconds(request.duration_seconds)
        if self._fixed_length_music(request.model):
            if abs(seconds - self._CLIP_MUSIC_SECONDS) > 1:
                notes.append(f"{request.model} makes {self._CLIP_MUSIC_SECONDS:.0f}-second clips: the track loops.")
        else:
            directions.append(f"Length: {length_label(seconds)} ({seconds:.0f} seconds), ending on a clear final chord.")
        if request.instrumental:
            directions.append("Instrumental only, no vocals.")
        if request.negative_prompt:
            directions.append(f"Avoid: {request.negative_prompt.strip()}.")
        body = {"model": request.model, "input": "\n".join(directions)[:4000]}
        try:
            async with httpx.AsyncClient(timeout=600) as http:
                response = await http.post(
                    f"{self._API_ROOT}/interactions", json=body, headers={"x-goog-api-key": self._api_key}
                )
        except httpx.HTTPError as exc:
            raise status_error(None, type(exc).__name__, vendor="Gemini") from None
        if response.status_code >= 400:
            raise status_error(response.status_code, _gemini_reason(response), vendor="Gemini")
        blocks = _interaction_outputs(response.json())
        audio = [b for b in blocks if b.get("type") == "audio" and b.get("data")]
        if not audio:
            raise MusicGenerationError("Lyria returned no music (the prompt may have been filtered).")
        text = "\n".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text").strip()
        mime = str(audio[-1].get("mime_type") or audio[-1].get("mimeType") or "audio/mpeg")
        return MusicResult(base64.b64decode(audio[-1]["data"]), mime, description=text[:4000], notes=notes)


def _interaction_outputs(body: object) -> list[dict]:
    """The content blocks of an interaction's ``model_output`` steps (Interactions API): audio, video, text."""
    steps = body.get("steps") if isinstance(body, dict) else None
    return [
        block
        for step in steps or []
        if isinstance(step, dict) and step.get("type") == "model_output"
        for block in step.get("content") or []
        if isinstance(block, dict)
    ]


def _gemini_reason(response) -> str:
    """The reason a Gemini REST answer gives for a refusal (``error.message``), ``""`` when it gives none."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ""
    return str((error.get("message") if isinstance(error, dict) else error) or "")


def _omni_state(body: object):
    """What a Gemini Omni interaction says of its clip: running, its video once completed, or why it has none."""
    import base64

    from app.llm.video import JobState

    status = str((body.get("status") if isinstance(body, dict) else None) or "")
    if status == "completed":
        videos = [b for b in _interaction_outputs(body) if b.get("type") == "video" and b.get("data")]
        if not videos:
            return JobState(True, error="the model returned no video (the prompt may have been filtered)")
        return JobState(True, data=base64.b64decode(videos[-1]["data"]))
    if status in ("failed", "cancelled"):
        error = body.get("error") if isinstance(body, dict) else None
        reason = error.get("message") if isinstance(error, dict) else error
        return JobState(True, error=str(reason or f"the interaction was {status}"))
    return JobState(False, state=status or "in_progress")


def _speech_error(message: str):
    from app.llm.speech import SpeechGenerationError

    return SpeechGenerationError(message)


def _raise_for_speech(response) -> None:
    """A refused speech call as a ``SpeechGenerationError`` naming Gemini's reason, never the key."""
    if response.status_code < 400:
        return
    reason = _gemini_reason(response)
    raise _speech_error(f"Gemini refused the speech request (HTTP {response.status_code}): {reason[:300]}".rstrip(": "))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def gemini_wire_tools(tools: list[ToolDefinition]) -> list[tuple[str, str, dict | None]]:
    """*tools* as Gemini declares them: ``(name, description, parameters)``, one per
    name (the last wins), each schema cleaned, no parameters without properties.
    Shared with routes that forward OpenAI-format tools to Gemini (the AI Gateway)."""
    seen: dict[str, tuple[str, str, dict | None]] = {}
    for t in tools:
        schema = _clean_schema_for_gemini(t.input_schema)
        seen[t.name] = (t.name, t.description, schema if schema.get("properties") else None)
    return list(seen.values())


def _gemini_tool(tools: list[ToolDefinition]) -> types.Tool:
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(name=name, description=description, parameters=parameters)
            for name, description, parameters in gemini_wire_tools(tools)
        ]
    )


def _clean_schema_for_gemini(schema: dict) -> dict:
    """Clean a JSON Schema for Gemini's function calling.

    Gemini's function calling has stricter schema requirements than
    OpenAI/Anthropic — it doesn't support ``default``, ``$schema``,
    or certain other JSON Schema keywords.  It also rejects empty
    strings inside ``enum`` arrays, and any ``enum`` value that is not a
    string: ``FunctionDeclaration`` refuses ``"enum": [1, 2]``, and one
    refused declaration fails every request of the agent carrying it. Such an
    enum is dropped and its values are said in the description instead, so
    the property keeps its own type and the arguments need no conversion.

    A type union is not a Gemini type either: ``"type": ["string", "null"]``
    and an ``anyOf`` with a ``{"type": "null"}`` variant become the non-null
    type with ``nullable: true``; any other union keeps its first non-null type.

    An array without ``items`` fails the whole request: it gets an empty
    ``items``, which Gemini takes as any element.
    """
    if not isinstance(schema, dict):
        return schema
    nullable = False
    union = schema.get("anyOf")
    if "type" not in schema and isinstance(union, list):
        variants = [variant for variant in union if isinstance(variant, dict)]
        nullable = any(_is_null_type(variant.get("type")) for variant in variants)
        # A union Gemini cannot declare: its first non-null typed variant, under the union's own description.
        first = next(
            (variant for variant in variants if "type" in variant and not _is_null_type(variant["type"])),
            None,
        )
        if first is not None:
            schema = {**first, **({"description": schema["description"]} if "description" in schema else {})}
    declared = schema.get("type")
    if isinstance(declared, list):
        kinds = [kind for kind in declared if not _is_null_type(kind)]
        nullable = nullable or len(kinds) < len(declared)
        schema = {**schema, "type": kinds[0] if kinds else "null"}

    cleaned: dict = {}
    # Keys that Gemini supports in function parameter schemas
    allowed_keys = {"type", "properties", "required", "description", "items", "enum", "nullable"}

    for key, value in schema.items():
        if key not in allowed_keys:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {k: _clean_schema_for_gemini(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            cleaned[key] = _clean_schema_for_gemini(value)
        elif key == "enum" and isinstance(value, list):
            filtered = [v for v in value if v != ""]
            if filtered and all(isinstance(v, str) for v in filtered):
                cleaned[key] = filtered
            elif filtered:
                allowed = "One of: " + ", ".join(json.dumps(v) for v in filtered) + "."
                described = schema.get("description")
                cleaned["description"] = f"{described} {allowed}" if described else allowed
        elif key == "description" and "description" in cleaned:
            continue  # already written with the allowed values of a non-string enum
        else:
            cleaned[key] = value

    if cleaned.get("type") == "array" and not isinstance(cleaned.get("items"), dict):
        cleaned["items"] = {}
    if nullable and cleaned.get("type") not in (None, "null"):
        cleaned["nullable"] = True
    return cleaned


def _is_null_type(kind: object) -> bool:
    """``"null"`` in a JSON Schema ``type``, alone or as ``["null"]``."""
    return kind == "null" or kind == ["null"]
