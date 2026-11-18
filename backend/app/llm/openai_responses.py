# SPDX-License-Identifier: Apache-2.0

"""Chat calls with function tools sent through OpenAI's Responses API.

A model the capability table flags ``responses_api_with_tools``
(``model_capabilities.uses_responses_api_with_tools``: gpt-6-astra, gpt-6.1-sol, gpt-6.2-sol,
gpt-6.2-astra) has
no valid request with function tools on ``/v1/chat/completions``: an effort
next to tools is refused with "use /v1/responses or set reasoning_effort to
'none'", and ``none`` itself is refused. ``/v1/responses`` takes the tools and
an effort together, so the native OpenAI provider sends those requests here.

The request is built from the same OpenAI chat messages the chat path builds
(``build_openai_messages_async``) and the answer is the same
:class:`ChatResponse`: text, tool calls, reasoning summary, usage in the chat
keys (``prompt_tokens`` / ``completion_tokens`` / ``prompt_tokens_details``),
stop reason and the ``content_filter`` decline.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.llm.base import ChatResponse, declined_error, parse_tool_arguments, retry_llm_call, usage_sink_of
from app.llm.model_capabilities import (
    build_openai_generation_params,
    lowest_effort,
    refuses_effort_none,
)
from app.llm.openai_compat import create_without_refused_temperature
from app.tools.base import ToolCall, ToolDefinition

logger = logging.getLogger(__name__)

#: A caller's "reasoning off" values.
_EFFORT_OFF_VALUES = frozenset({"none", "minimal"})


def _field(obj: object, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def responses_effort(effort: str | None, model: str | None) -> str | None:
    """The ``reasoning.effort`` a Responses API call of *model* sends for *effort*.

    The chat mapping (``normalize_openai_reasoning_effort``) applies, without
    its tools rule: this endpoint takes an effort next to tools. A model that
    refuses ``none`` gets its lowest declared level for ``none``, ``minimal``
    or no effort at all, as its default effort is not guaranteed either.
    """
    from app.llm.openai_compat import normalize_openai_reasoning_effort

    mapped = normalize_openai_reasoning_effort(effort, model)
    if refuses_effort_none(model) and (mapped is None or mapped in _EFFORT_OFF_VALUES):
        return lowest_effort(model)
    return mapped


def _text_of(content: object) -> str:
    """The text of a chat message content: a string, or the ``text`` of its parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(_field(part, "text") or "") for part in content if _field(part, "type") == "text")
    return ""


def _input_part(part: object) -> dict[str, Any] | None:
    """One chat content part as a Responses API input part."""
    kind = _field(part, "type")
    if kind == "text":
        return {"type": "input_text", "text": str(_field(part, "text") or "")}
    if kind == "image_url":
        image = _field(part, "image_url")
        url = _field(image, "url") if not isinstance(image, str) else image
        if not url:
            return None
        converted: dict[str, Any] = {"type": "input_image", "image_url": url}
        detail = _field(image, "detail") if not isinstance(image, str) else None
        if detail:
            converted["detail"] = detail
        return converted
    if kind == "file":
        file = _field(part, "file") or {}
        converted = {"type": "input_file"}
        for key in ("file_data", "file_id", "filename"):
            if value := _field(file, key):
                converted[key] = value
        return converted
    return None


def responses_input(oai_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The Responses API ``input`` items of OpenAI chat messages.

    System and user turns are messages (a user's image parts as
    ``input_image``), an assistant's tool calls are ``function_call`` items
    and a tool result is a ``function_call_output`` keyed by its call id.
    """
    items: list[dict[str, Any]] = []
    for message in oai_messages:
        role = message.get("role")
        content = message.get("content")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id") or "",
                    "output": _text_of(content),
                }
            )
            continue
        if role == "assistant":
            if text := _text_of(content):
                items.append({"role": "assistant", "content": text})
            for call in message.get("tool_calls") or ():
                function = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id") or "",
                        "name": function.get("name") or "",
                        "arguments": function.get("arguments") or "{}",
                    }
                )
            continue
        if isinstance(content, list):
            parts = [converted for part in content if (converted := _input_part(part)) is not None]
            items.append({"role": role, "content": parts})
        else:
            items.append({"role": role, "content": content or ""})
    return items


def responses_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """Function tools in the Responses API shape (flat, not under ``function``)."""
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
            "strict": False,
        }
        for tool in tools
    ]


def responses_text_format(structured_output: object) -> dict[str, Any] | None:
    """A chat ``response_format`` as the Responses API ``text`` parameter."""
    if not isinstance(structured_output, Mapping):
        return None
    kind = structured_output.get("type")
    if kind == "json_schema":
        schema = structured_output.get("json_schema") or {}
        text_format: dict[str, Any] = {
            "type": "json_schema",
            "name": schema.get("name") or "response",
            "schema": schema.get("schema") or {},
        }
        if "strict" in schema:
            text_format["strict"] = schema["strict"]
        return {"format": text_format}
    if kind == "json_object":
        return {"format": {"type": "json_object"}}
    return None


def build_responses_params(
    model: str,
    oai_messages: list[dict[str, Any]],
    tools: list[ToolDefinition] | None,
    *,
    temperature: float | None,
    max_tokens: int,
    reasoning_effort: str | None,
    structured_output: object = None,
) -> dict[str, Any]:
    """The ``responses.create`` parameters of one chat call."""
    generation = build_openai_generation_params(model, temperature=temperature, max_tokens=max_tokens)
    params: dict[str, Any] = {"model": model, "input": responses_input(oai_messages), "store": False}
    params["max_output_tokens"] = max_tokens
    if "temperature" in generation:
        params["temperature"] = generation["temperature"]
    if tools:
        params["tools"] = responses_tools(tools)
    if effort := responses_effort(reasoning_effort, model):
        params["reasoning"] = {"effort": effort, "summary": "auto"}
    if text := responses_text_format(structured_output):
        params["text"] = text
    return params


def responses_usage(usage: object) -> dict[str, Any]:
    """A Responses API usage in the keys the chat path reports and prices."""
    if usage is None:
        return {}
    result: dict[str, Any] = {
        "prompt_tokens": int(_field(usage, "input_tokens") or 0),
        "completion_tokens": int(_field(usage, "output_tokens") or 0),
    }
    total = _field(usage, "total_tokens")
    if isinstance(total, int):
        result["total_tokens"] = total
    cached = _field(_field(usage, "input_tokens_details"), "cached_tokens")
    if isinstance(cached, int) and cached:
        result["prompt_tokens_details"] = {"cached_tokens": cached}
    reasoning = _field(_field(usage, "output_tokens_details"), "reasoning_tokens")
    if isinstance(reasoning, int) and reasoning:
        result["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return result


def parse_responses_output(response: object, model: str) -> ChatResponse:
    """A Responses API response as a :class:`ChatResponse`."""
    usage = responses_usage(_field(response, "usage"))
    incomplete = _field(_field(response, "incomplete_details"), "reason")
    if incomplete == "content_filter":
        raise declined_error(model, None, usage)
    text: list[str] = []
    thinking: list[str] = []
    calls: list[ToolCall] = []
    for item in _field(response, "output") or ():
        kind = _field(item, "type")
        if kind == "message":
            for part in _field(item, "content") or ():
                if _field(part, "type") == "output_text":
                    text.append(str(_field(part, "text") or ""))
        elif kind == "function_call":
            name = str(_field(item, "name") or "")
            calls.append(
                ToolCall(
                    id=str(_field(item, "call_id") or _field(item, "id") or ""),
                    name=name,
                    arguments=parse_tool_arguments(str(_field(item, "arguments") or ""), tool_name=name),
                )
            )
        elif kind == "reasoning":
            for part in _field(item, "summary") or ():
                if summary := _field(part, "text"):
                    thinking.append(str(summary))
    if calls:
        stop_reason = "tool_use"
    elif incomplete == "max_output_tokens":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"
    return ChatResponse(
        content="".join(text),
        model=str(_field(response, "model") or model),
        usage=usage,
        tool_calls=calls or None,
        stop_reason=stop_reason,
        thinking="\n".join(thinking) if thinking else None,
    )


async def responses_chat(
    client: Any,
    *,
    model: str,
    oai_messages: list[dict[str, Any]],
    tools: list[ToolDefinition] | None,
    temperature: float | None = 0.7,
    max_tokens: int = 4096,
    is_retryable: Callable[[Exception], bool],
    on_token: Callable[[str], Awaitable[None]] | None = None,
    stream: bool = False,
    **kwargs: Any,
) -> ChatResponse:
    """One chat call through ``client.responses.create``.

    With *stream*, text deltas reach *on_token* while no function call has
    started and reasoning summary deltas reach ``on_thinking``; the answer is
    read off the final ``response.completed`` (or ``response.incomplete``)
    event. Transient errors are retried only while nothing has been emitted.
    """
    params = build_responses_params(
        model,
        oai_messages,
        tools,
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=kwargs.get("reasoning_effort"),
        structured_output=kwargs.get("structured_output"),
    )
    on_thinking = kwargs.get("on_thinking")
    usage_sink = usage_sink_of(kwargs)
    emitted = False

    async def _once() -> ChatResponse:
        nonlocal emitted
        if usage_sink is not None:
            usage_sink.restart()
        if not stream:
            answer = parse_responses_output(
                await create_without_refused_temperature(lambda p: client.responses.create(**p), params, model),
                model,
            )
            if usage_sink is not None and answer.usage:
                usage_sink.report(answer.usage)
            return answer
        events = await create_without_refused_temperature(
            lambda p: client.responses.create(**p, stream=True), params, model
        )
        final: object = None
        text: list[str] = []
        calling = False
        async for event in events:
            kind = _field(event, "type")
            if kind == "response.output_text.delta":
                delta = str(_field(event, "delta") or "")
                text.append(delta)
                if delta and on_token and not calling:
                    emitted = True
                    await on_token(delta)
            elif kind == "response.reasoning_summary_text.delta":
                delta = str(_field(event, "delta") or "")
                if delta and on_thinking:
                    emitted = True
                    await on_thinking(delta)
            elif kind == "response.output_item.added":
                if _field(_field(event, "item"), "type") == "function_call":
                    calling = True
            elif kind in ("response.completed", "response.incomplete"):
                final = _field(event, "response")
            elif kind in ("response.failed", "error"):
                error = _field(_field(event, "response"), "error") or event
                raise RuntimeError(
                    f"Responses API stream failed for {model}: "
                    f"{_field(error, 'code') or ''} {_field(error, 'message') or ''}".strip()
                )
        if final is None:
            return ChatResponse(content="".join(text), model=model)
        answer = parse_responses_output(final, model)
        if usage_sink is not None and answer.usage:
            usage_sink.report(answer.usage)
        return answer

    logger.debug("Sending %s with %d tool(s) through the Responses API", model, len(tools or ()))
    return await retry_llm_call(_once, is_retryable=lambda exc: not emitted and is_retryable(exc))


__all__ = [
    "build_responses_params",
    "parse_responses_output",
    "responses_chat",
    "responses_effort",
    "responses_input",
    "responses_text_format",
    "responses_tools",
    "responses_usage",
]
