# SPDX-License-Identifier: Apache-2.0

"""AWS Bedrock LLM provider.

Amazon Bedrock provides access to foundation models from Anthropic, Meta,
Mistral, and others through AWS infrastructure.  Implemented with ``boto3``
against the Bedrock Runtime **Converse / ConverseStream** APIs (the boto3
calls are executed in a thread-pool executor so they never block the event
loop).

The API key format is:
``aws_access_key_id||aws_secret_access_key||aws_region``

Example: ``AKIAIOSFODNN7EXAMPLE||wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY||us-east-1``

A region-only key selects the ambient AWS credential chain (IAM role).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable

from app.core.log_once import fingerprint, log_once_soon
from app.llm.base import (
    TOOL_RESULT_IMAGE_DATA_NOTE,
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    FileRef,
    detect_image_mime,
    parse_tool_arguments,
    retry_llm_call,
    tool_results_in_call_order,
    usage_sink_of,
)
from app.llm.file_resolver import (
    FallbackText,
    NativeFile,
    PayloadFilePlan,
    file_kind,
    prepare_tool_result_attachments,
    resolve_file,
    resolve_payload_files,
)
from app.llm.model_capabilities import (
    is_claude_model,
    omit_temperature,
    supports_bedrock_structured_output,
)
from app.llm.tool_markup_recovery import recover_leaked_tool_calls
from app.tools.base import ToolCall, ToolDefinition

logger = logging.getLogger(__name__)

# A model Bedrock serves without structured outputs is a standing condition of
# the deployment: one line a day for the whole platform is enough to surface it.
_NO_STRUCTURED_OUTPUT_NOTICE_INTERVAL_SECONDS = 24 * 60 * 60

_BEDROCK_DOC_FORMATS: dict[str, str] = {
    "application/pdf": "pdf",
    "text/csv": "csv",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/html": "html",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}

_DOCUMENT_NAME_ID_RE = re.compile(r"[^A-Za-z0-9-]+")


def _document_name(file_ref: FileRef) -> str:
    """The ``name`` of a Converse document block: the file's id and kind, never its stored name.

    The model reads the field as part of the prompt and it must be a plain
    string (alphanumerics, single spaces, hyphens, parentheses, brackets, at
    most 200 characters), so it cannot hold an untrusted block: the stored
    name - often a third party's text - stays out of it, as it stays out of
    every other provider's native document block.
    """
    file_id = _DOCUMENT_NAME_ID_RE.sub("-", str(file_ref.file_id or "")).strip("-")[:160] or "attachment"
    return f"File {file_id} ({file_kind(file_ref.filename)})"


def _is_retryable_bedrock(exc: Exception) -> bool:
    """Return True for transient Bedrock errors worth retrying.

    Prefers the structured botocore error code when available — free-text
    substring matching on ``"rate"`` also matches words like "generate" /
    "operate" inside permanent 400 validation messages, which used to
    retry non-transient failures five times with backoff.
    """
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code", "")
    if code:
        return code in (
            "ThrottlingException",
            "TooManyRequestsException",
            "ServiceUnavailableException",
            "InternalServerException",
            "ModelTimeoutException",
            "ModelNotReadyException",
            "RequestTimeout",
        )
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in (
            "throttling",
            "rate limit",
            "too many requests",
            "timeout",
            "503",
            "429",
            "service unavailable",
        )
    )


def _usage_from_bedrock(usage_info: dict | None) -> dict:
    """Normalize a Bedrock Converse ``usage`` dict to the standard usage dict.

    Mirrors the non-streaming ``chat()`` usage shape (``inputTokens`` /
    ``outputTokens`` → ``prompt_tokens`` / ``completion_tokens``) so streamed
    turns are costed identically in Langfuse.  Returns ``{}`` when no usage
    is present so "no usage" stays falsy downstream.
    """
    if not usage_info:
        return {}
    prompt = usage_info.get("inputTokens", 0) or 0
    completion = usage_info.get("outputTokens", 0) or 0
    # A prompt read entirely from the cache reports 0 ``inputTokens``: the
    # cache counters alone still make it a billed call.
    cache = _bedrock_cache_usage(usage_info)
    if not (prompt or completion or cache):
        return {}
    return {"prompt_tokens": prompt, "completion_tokens": completion, **cache}


def _bedrock_cache_usage(usage_info: dict) -> dict:
    """The Converse prompt-cache counters, under the keys the pricing reads.

    Like Anthropic's, Bedrock's ``inputTokens`` leaves the cache reads and
    writes out, so without these the cached part of a prompt is billed by
    Bedrock and priced $0 by the run budget and the traces. ``totalTokens`` is
    deliberately not carried: the token budget counts a Bedrock call like an
    Anthropic one, without its cache tokens.
    """
    usage: dict[str, int] = {}
    for source, key in (
        ("cacheReadInputTokens", "cache_read_input_tokens"),
        ("cacheWriteInputTokens", "cache_creation_input_tokens"),
    ):
        count = usage_info.get(source)
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            usage[key] = count
    return usage


def _build_inference_config(
    model: str, temperature: float, max_tokens: int
) -> dict[str, float | int]:
    config: dict[str, float | int] = {"maxTokens": max_tokens}
    if not omit_temperature(model):
        config["temperature"] = temperature
    return config


def _apply_structured_output(params: dict, model: str, structured_output: dict | None) -> None:
    """Send *structured_output* as ``outputConfig`` where Bedrock offers it for *model*.

    A model Bedrock serves without structured outputs (Claude Sonnet 5.5) is
    sent none: its schema is enforced by the prompt-based SAP pipeline
    (``app.services.structured_output``) alone, as on any provider without
    native structured output, which an operator is told once a day.
    """
    if not structured_output:
        return
    if supports_bedrock_structured_output(model):
        params["outputConfig"] = structured_output
        return
    log_once_soon(
        logger,
        logging.WARNING,
        f"bedrock_no_structured_output:{fingerprint(model)}",
        "Bedrock serves model '%s' without structured outputs: a JSON schema "
        "set on it is enforced by the prompt-based schema instructions and "
        "validation alone. The Claude API serves the model with them.",
        model,
        interval=_NO_STRUCTURED_OUTPUT_NOTICE_INTERVAL_SECONDS,
    )


def _is_blank_text(block: dict) -> bool:
    return block.keys() == {"text"} and not block["text"]


def _alternating(converse_messages: list[dict]) -> list[dict]:
    """*converse_messages* with every run of one role merged into one message.

    Converse refuses a conversation that does not alternate between ``user``
    and ``assistant``, and a transcript holds such runs: a message right after
    the tool results it follows (steering, a resumed turn's work), a
    mid-turn segment stored before the answer that continued it. The blocks
    keep their order, so tool results stay first in their user message; a
    blank text block is dropped from a merged message that holds others.
    """
    merged: list[dict] = []
    for message in converse_messages:
        if not merged or merged[-1]["role"] != message["role"]:
            merged.append(message)
            continue
        blocks = [*merged[-1]["content"], *message["content"]]
        kept = [block for block in blocks if not _is_blank_text(block)]
        merged[-1] = {"role": message["role"], "content": kept or blocks[:1]}
    return merged


class BedrockProvider(BaseLLMProvider):
    """AWS Bedrock — managed access to foundation models."""

    provider_name = "bedrock"
    # Default chat model used by chat / chat_stream / chat_stream_with_tools
    # when no explicit model is provided.  Exposed as a class attribute so
    # the FailoverLLMProvider can resolve per-model max_output_tokens
    # overrides for fallback calls that arrive with model=None.
    default_model = "anthropic.claude-sonnet-5"
    context_window = 1_000_000
    max_output_tokens = 8_192
    # Bedrock hosts models from multiple vendors (Anthropic Claude, Amazon
    # Titan, etc.).  The Converse API itself does not enforce 128 but the
    # underlying model may.  Use a moderate cap above OpenAI's 128.
    max_tools = 256

    # ``chat_stream()`` (ConverseStream) forwards ``structured_output`` as
    # ``outputConfig`` just like the non-streaming Converse path, so the
    # agentic loop may forward the kwarg to streaming calls.
    supports_streaming_structured_output: bool = True

    def __init__(self, api_key: str, **kwargs):
        """Initialize with AWS credentials.

        api_key format: ``access_key||secret_key||region``
        """
        parts = api_key.split("||")
        if len(parts) >= 3:
            self.aws_access_key = parts[0].strip()
            self.aws_secret_key = parts[1].strip()
            self.aws_region = parts[2].strip()
        elif len(parts) == 2:
            self.aws_access_key = parts[0].strip()
            self.aws_secret_key = parts[1].strip()
            self.aws_region = "us-east-1"
        else:
            # Treat as region-only (for IAM role auth)
            self.aws_access_key = ""
            self.aws_secret_key = ""
            self.aws_region = api_key.strip() if api_key.strip() else "us-east-1"

        self._client = None

    def _build_client(self):
        """Construct the boto3 bedrock-runtime client (synchronous)."""
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "boto3 is required for the Bedrock provider. Install it with: pip install boto3"
            ) from exc

        from botocore.config import Config as BotoConfig

        client_kwargs = {
            "service_name": "bedrock-runtime",
            "region_name": self.aws_region,
            "config": BotoConfig(
                connect_timeout=10,
                read_timeout=600,
                retries={"max_attempts": 2},
            ),
        }
        if self.aws_access_key and self.aws_secret_key:
            client_kwargs["aws_access_key_id"] = self.aws_access_key
            client_kwargs["aws_secret_access_key"] = self.aws_secret_key

        return boto3.client(**client_kwargs)

    async def _get_client(self):
        """Lazy-initialize the boto3 bedrock-runtime client off the event loop.

        ``boto3.client(...)`` resolves the credential chain synchronously —
        when no explicit keys are configured that can include IMDS metadata
        HTTP calls, which would block the event loop. Build in a thread.
        """
        if self._client is None:
            self._client = await asyncio.to_thread(self._build_client)
        return self._client

    # ------------------------------------------------------------------
    # Message building (shared by chat, chat_stream_with_tools)
    # ------------------------------------------------------------------

    async def _prepare_request(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDefinition] | None,
        model: str,
    ) -> tuple[list[dict], list[dict]]:
        """Resolve attachments, build the payload and run the overflow check.

        The one path for ``chat``, ``chat_stream`` and
        ``chat_stream_with_tools``: attachments resolved together
        under Bedrock's page cap and a share of *model*'s context window, the
        native PDFs costed by their pages in the overflow check while the
        other document types (csv, docx, xlsx...) keep the byte proxy.
        """
        plan = await resolve_payload_files(
            messages,
            "bedrock",
            context_window=self._resolve_context_window(model),
        )
        system_parts, converse_messages = await self._build_converse_messages(
            messages, file_plan=plan
        )
        text_chars, image_count, native_file_bytes = self._measure_bedrock_payload(
            system_parts, converse_messages
        )
        self._check_resolved_overflow(
            text_chars=text_chars,
            image_count=image_count,
            native_file_bytes=max(native_file_bytes - plan.native_pdf_bytes, 0),
            native_document_tokens=plan.native_pdf_tokens,
            tools=tools,
            model=model,
        )
        return system_parts, converse_messages

    async def _build_converse_messages(
        self,
        messages: list[ChatMessage],
        *,
        file_plan: PayloadFilePlan | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """Convert generic ChatMessages to Bedrock Converse format.

        Returns ``(system_parts, converse_messages)``. *file_plan* is the
        request-wide attachment resolution (:func:`resolve_payload_files`),
        built here when the caller has none.
        """
        if file_plan is None:
            file_plan = await resolve_payload_files(messages, "bedrock")
        system_parts: list[dict] = []
        converse_messages: list[dict] = []

        for m_idx, msg in enumerate(tool_results_in_call_order(messages)):
            if msg.role == "system":
                system_parts.append({"text": msg.content})
            elif msg.role == "user":
                user_content: list[dict] = [{"text": msg.content}]
                if msg.images:
                    for img in msg.images:
                        if not img.startswith(("http://", "https://")):
                            mime = detect_image_mime(img)
                            fmt = "jpeg"
                            if "png" in mime:
                                fmt = "png"
                            elif "gif" in mime:
                                fmt = "gif"
                            elif "webp" in mime:
                                fmt = "webp"
                            user_content.append(
                                {
                                    "image": {
                                        "format": fmt,
                                        "source": {"bytes": base64.b64decode(img)},
                                    }
                                }
                            )
                if msg.files:
                    for f_idx, file_ref in enumerate(msg.files):
                        resolved = file_plan.get(m_idx, f_idx)
                        if resolved is None:
                            resolved = await resolve_file(file_ref, "bedrock")
                        if isinstance(resolved, NativeFile):
                            if resolved.content_type.startswith("image/"):
                                fmt = "jpeg"
                                if "png" in resolved.content_type:
                                    fmt = "png"
                                elif "gif" in resolved.content_type:
                                    fmt = "gif"
                                elif "webp" in resolved.content_type:
                                    fmt = "webp"
                                user_content.append(
                                    {
                                        "image": {
                                            "format": fmt,
                                            "source": {"bytes": resolved.data},
                                        }
                                    }
                                )
                            elif resolved.content_type in _BEDROCK_DOC_FORMATS:
                                user_content.append(
                                    {
                                        "document": {
                                            "name": _document_name(file_ref),
                                            "format": _BEDROCK_DOC_FORMATS[
                                                resolved.content_type
                                            ],
                                            "source": {"bytes": resolved.data},
                                        }
                                    }
                                )
                        elif isinstance(resolved, FallbackText):
                            user_content.append({"text": resolved.text})
                converse_messages.append({"role": "user", "content": user_content})
            elif msg.role == "assistant":
                content_blocks: list[dict] = []
                if msg.content:
                    content_blocks.append({"text": msg.content})
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        content_blocks.append(
                            {
                                "toolUse": {
                                    "toolUseId": tc.id,
                                    "name": tc.name,
                                    "input": tc.arguments,
                                }
                            }
                        )
                converse_messages.append(
                    {
                        "role": "assistant",
                        "content": content_blocks or [{"text": ""}],
                    }
                )
            elif msg.role == "tool":
                result_content: list[dict] = [{"text": msg.content}]
                if msg.files:
                    images, extra_texts = await prepare_tool_result_attachments(
                        msg.files,
                        "bedrock",
                        source=f"bedrock:tool_result:{msg.tool_call_id}",
                    )
                    if extra_texts:
                        result_content[0]["text"] = (
                            (msg.content + "\n\n" if msg.content else "")
                            + "\n\n".join(extra_texts)
                        )
                    for img in images:
                        fmt = "jpeg"
                        if "png" in img.mime:
                            fmt = "png"
                        elif "gif" in img.mime:
                            fmt = "gif"
                        elif "webp" in img.mime:
                            fmt = "webp"
                        result_content.append(
                            {
                                "image": {
                                    "format": fmt,
                                    "source": {"bytes": img.data},
                                }
                            }
                        )
                    if images and TOOL_RESULT_IMAGE_DATA_NOTE:
                        first = result_content[0]["text"]
                        result_content[0]["text"] = (
                            (first + "\n\n" if first else "") + TOOL_RESULT_IMAGE_DATA_NOTE
                        )
                tool_result = {
                    "toolResult": {
                        "toolUseId": msg.tool_call_id,
                        "content": result_content,
                    }
                }
                if (
                    converse_messages
                    and converse_messages[-1]["role"] == "user"
                    and converse_messages[-1]["content"]
                    and "toolResult" in converse_messages[-1]["content"][-1]
                ):
                    converse_messages[-1]["content"].append(tool_result)
                else:
                    converse_messages.append(
                        {"role": "user", "content": [tool_result]}
                    )

        return system_parts, _alternating(converse_messages)

    @staticmethod
    def _measure_bedrock_payload(
        system_parts: list[dict],
        converse_messages: list[dict],
    ) -> tuple[int, int, int]:
        """Measure a Bedrock Converse payload after file resolution.

        Returns (text_chars, image_count, native_file_bytes).
        """
        text_chars = sum(len(p.get("text", "")) for p in system_parts)
        image_count = 0
        native_file_bytes = 0

        for msg in converse_messages:
            for block in msg.get("content", []):
                if "text" in block:
                    text_chars += len(block["text"])
                elif "image" in block:
                    image_count += 1
                elif "document" in block:
                    source = block["document"].get("source", {})
                    data = source.get("bytes", b"")
                    native_file_bytes += len(data) if isinstance(data, bytes) else 0
                elif "toolUse" in block:
                    text_chars += len(json.dumps(block["toolUse"].get("input", {})))
                elif "toolResult" in block:
                    for inner in block["toolResult"].get("content", []):
                        if "text" in inner:
                            text_chars += len(inner.get("text", ""))
                        elif "image" in inner:
                            image_count += 1
                            source = inner["image"].get("source", {})
                            data = source.get("bytes", b"")
                            native_file_bytes += (
                                len(data) if isinstance(data, bytes) else 0
                            )

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
        """Call Bedrock's Converse API."""
        model = model or self.default_model

        client = await self._get_client()

        system_parts, converse_messages = await self._prepare_request(
            messages, tools, model
        )

        converse_params: dict = {
            "modelId": model,
            "messages": converse_messages,
            "inferenceConfig": _build_inference_config(model, temperature, max_tokens),
        }
        if system_parts:
            converse_params["system"] = system_parts

        if tools:
            tool_config = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": {"json": t.input_schema},
                        }
                    }
                    for t in tools
                ]
            }
            converse_params["toolConfig"] = tool_config

        # Structured output → Bedrock outputConfig.textFormat
        _apply_structured_output(converse_params, model, kwargs.get("structured_output"))

        # Run synchronous boto3 call in executor
        async def _call():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, lambda: client.converse(**converse_params)
            )

        response = await retry_llm_call(_call, is_retryable=_is_retryable_bedrock)

        # Parse response
        text_parts: list[str] = []
        tool_calls_list: list[ToolCall] = []

        for block in response.get("output", {}).get("message", {}).get("content", []):
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls_list.append(
                    ToolCall(
                        id=tu["toolUseId"],
                        name=tu["name"],
                        arguments=tu.get("input", {}),
                    )
                )

        raw_stop = response.get("stopReason", "end_turn")
        if raw_stop == "tool_use":
            stop_reason = "tool_use"
        elif raw_stop == "max_tokens":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"

        usage_info = response.get("usage", {})

        # Recover a tool call a Claude model emitted as text (see
        # ``app.llm.tool_markup_recovery``). The markup is Claude's own, so the
        # other families Bedrock serves (Llama, Mistral, Nova) are left alone.
        content = "\n".join(text_parts)
        leaked_unrecovered = False
        recovery = recover_leaked_tool_calls(
            content, tool_calls_list, tools if is_claude_model(model) else None
        )
        if recovery is not None:
            if recovery.tool_calls:
                tool_calls_list = recovery.tool_calls
                content = recovery.content
                stop_reason = "tool_use"
            leaked_unrecovered = recovery.unrecovered

        return ChatResponse(
            content=content,
            model=model,
            usage={
                "prompt_tokens": usage_info.get("inputTokens", 0),
                "completion_tokens": usage_info.get("outputTokens", 0),
                **_bedrock_cache_usage(usage_info),
            },
            tool_calls=tool_calls_list if tool_calls_list else None,
            stop_reason=stop_reason,
            leaked_tool_markup_unrecovered=leaked_unrecovered,
        )

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Stream from Bedrock's ConverseStream API."""
        model = model or self.default_model
        usage_sink = usage_sink_of(kwargs)
        if usage_sink is not None:
            usage_sink.restart()

        client = await self._get_client()

        system_parts, converse_messages = await self._prepare_request(
            messages, tools, model
        )

        params: dict = {
            "modelId": model,
            "messages": converse_messages,
            "inferenceConfig": _build_inference_config(model, temperature, max_tokens),
        }
        if system_parts:
            params["system"] = system_parts

        # Structured output → Bedrock outputConfig.textFormat
        _apply_structured_output(params, model, kwargs.get("structured_output"))

        loop = asyncio.get_running_loop()

        async def _initiate_stream():
            return await loop.run_in_executor(
                None, lambda: client.converse_stream(**params)
            )

        response = await retry_llm_call(
            _initiate_stream, is_retryable=_is_retryable_bedrock
        )

        self.last_stop_reason = None
        self.last_usage = None
        stream = response.get("stream", [])

        # boto3 EventStream iterates lazily over the network, so each
        # ``next()`` call is blocking I/O.  We offload each iteration to
        # the thread-pool executor to avoid blocking the asyncio loop.
        it = iter(stream)
        while True:
            event = await loop.run_in_executor(None, lambda: next(it, None))
            if event is None:
                break
            if "metadata" in event:
                self.last_usage = _usage_from_bedrock(
                    event["metadata"].get("usage", {})
                )
                if usage_sink is not None:
                    usage_sink.report(self.last_usage)
            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                text = delta.get("text", "")
                if text:
                    yield text
            elif "messageStop" in event:
                sr = event["messageStop"].get("stopReason", "")
                if sr == "max_tokens":
                    self.last_stop_reason = "max_tokens"
                else:
                    self.last_stop_reason = "end_turn"

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

        client = await self._get_client()

        system_parts, converse_messages = await self._prepare_request(
            messages, tools, model
        )

        params: dict = {
            "modelId": model,
            "messages": converse_messages,
            "inferenceConfig": _build_inference_config(model, temperature, max_tokens),
        }
        if system_parts:
            params["system"] = system_parts
        if tools:
            params["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": {"json": t.input_schema},
                        }
                    }
                    for t in tools
                ]
            }
        _apply_structured_output(params, model, kwargs.get("structured_output"))

        loop = asyncio.get_running_loop()

        async def _initiate_stream():
            return await loop.run_in_executor(
                None, lambda: client.converse_stream(**params)
            )

        response = await retry_llm_call(
            _initiate_stream, is_retryable=_is_retryable_bedrock
        )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        has_tool_calls = False
        stop_reason = "end_turn"
        current_tool: dict | None = None
        usage_info: dict = {}
        stream = response.get("stream", [])

        it = iter(stream)
        while True:
            event = await loop.run_in_executor(None, lambda: next(it, None))
            if event is None:
                break

            if "metadata" in event:
                # The ConverseStream ``metadata`` event carries the final
                # token usage; without it the response would report zero
                # tokens (and therefore zero cost) in Langfuse.
                usage_info = event["metadata"].get("usage", {}) or {}
                if usage_sink is not None:
                    usage_sink.report(_usage_from_bedrock(usage_info))

            elif "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    has_tool_calls = True
                    tu = start["toolUse"]
                    current_tool = {
                        "id": tu.get("toolUseId", ""),
                        "name": tu.get("name", ""),
                        "arguments": [],
                    }

            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    text = delta["text"]
                    if text:
                        text_parts.append(text)
                        if not has_tool_calls and on_token:
                            await on_token(text)
                elif "toolUse" in delta:
                    if current_tool is not None:
                        current_tool["arguments"].append(delta["toolUse"].get("input", ""))

            elif "contentBlockStop" in event:
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

            elif "messageStop" in event:
                sr = event["messageStop"].get("stopReason", "")
                if sr == "tool_use":
                    stop_reason = "tool_use"
                elif sr == "max_tokens":
                    stop_reason = "max_tokens"
                else:
                    stop_reason = "end_turn"

        # Recover a tool call streamed as text (see the non-streaming path).
        content = "".join(text_parts)
        leaked_unrecovered = False
        recovery = recover_leaked_tool_calls(
            content, tool_calls, tools if is_claude_model(model) else None
        )
        if recovery is not None:
            if recovery.tool_calls:
                tool_calls = recovery.tool_calls
                content = recovery.content
                stop_reason = "tool_use"
            leaked_unrecovered = recovery.unrecovered

        return ChatResponse(
            content=content,
            model=model,
            usage=_usage_from_bedrock(usage_info),
            tool_calls=tool_calls if tool_calls else None,
            stop_reason=stop_reason,
            leaked_tool_markup_unrecovered=leaked_unrecovered,
        )

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """Bedrock embeddings via Titan or Cohere models."""
        model = model or "amazon.titan-embed-text-v2:0"
        client = await self._get_client()

        body = json.dumps({"inputText": text})
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.invoke_model(
                modelId=model,
                contentType="application/json",
                accept="application/json",
                body=body,
            ),
        )
        body_stream = response["body"]
        try:
            result = json.loads(body_stream.read())
        finally:
            body_stream.close()
        return result.get("embedding", [])

    async def list_models(self) -> list[str]:
        """List available Bedrock foundation models."""

        def _build_bedrock_client():
            import boto3

            client_kwargs = {
                "service_name": "bedrock",
                "region_name": self.aws_region,
            }
            if self.aws_access_key and self.aws_secret_key:
                client_kwargs["aws_access_key_id"] = self.aws_access_key
                client_kwargs["aws_secret_access_key"] = self.aws_secret_key
            return boto3.client(**client_kwargs)

        # Client construction resolves the credential chain (possibly IMDS
        # HTTP calls) — keep it off the event loop like the runtime client.
        bedrock_client = await asyncio.to_thread(_build_bedrock_client)
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, bedrock_client.list_foundation_models
        )

        models: list[str] = []
        for m in response.get("modelSummaries", []):
            model_id = m.get("modelId", "")
            output_modalities = m.get("outputModalities", [])
            if "TEXT" in output_modalities:
                models.append(model_id)

        return sorted(models)

    async def list_embedding_models(self) -> list[str]:
        """Return common Bedrock embedding models."""
        return [
            "amazon.titan-embed-text-v2:0",
            "amazon.titan-embed-text-v1",
            "cohere.embed-english-v3",
            "cohere.embed-multilingual-v3",
        ]
