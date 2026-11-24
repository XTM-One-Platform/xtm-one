# SPDX-License-Identifier: Apache-2.0

"""OpenAI-compatible LLM provider base class.

Many LLM providers expose an OpenAI-compatible API (same endpoints, same
message/tool format).  This module provides a reusable base class that
configures the OpenAI SDK with a custom ``base_url`` and optional defaults.

Also exports shared OpenAI SDK helpers, including retry handling and reasoning
effort normalization, used by the native ``OpenAIProvider``.

Used by: OpenRouter, Ollama, Mistral, SpaceXAI, Azure OpenAI, LiteLLM,
Custom OpenAI.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, NamedTuple

import httpx
import openai
from openai import AsyncOpenAI

from app.core.url_identity import redact_url
from app.llm.anthropic_thinking_replay import refuses_replayed_blocks, says_prompt_too_long
from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    ModelDeclinedError,
    build_openai_messages_async,
    declined_error,
    gateway_error_envelope,
    parse_tool_arguments,
    retry_llm_call,
    told_not_to_retry,
    usage_sink_of,
)
from app.llm.model_capabilities import (
    EFFORT_LEVELS,
    build_openai_generation_params,
    is_claude_model,
    lowest_effort,
    refuses_effort_none,
    refuses_effort_with_tools,
    supported_effort,
    takes_no_effort,
)
from app.llm.model_limits import _normalize_family_key, reported_context_window
from app.llm.openai_thinking_blocks import (
    BlocksReplay,
    blocks_of,
    refuses_replayed_signatures,
    thought_signature_of,
)
from app.llm.speech import OpenAISpeechMixin
from app.llm.video import OpenAIVideosMixin
from app.tools.base import ToolCall, ToolDefinition

logger = logging.getLogger(__name__)

# Where OpenAI-compatible servers put a model's context window in their
# ``/models`` listing, probed in this order: OpenRouter (``context_length``),
# Mistral (``max_context_length``), vLLM (``max_model_len``), Groq
# (``context_window``), LiteLLM-style metadata (``max_input_tokens``).
_LISTED_CONTEXT_WINDOW_FIELDS = (
    "context_length",
    "max_context_length",
    "max_model_len",
    "context_window",
    "max_input_tokens",
)


# ---------------------------------------------------------------------------
# Shared OpenAI SDK helpers
# ---------------------------------------------------------------------------
# These are the shared OpenAI SDK helpers imported by ``openai_provider`` so
# retry and reasoning behavior is never duplicated.


def marked_not_retryable(body: object) -> bool:
    """True when an error body, or its ``error`` envelope, says ``retryable: false``."""
    return gateway_error_envelope(body).get("retryable") is False


def is_retryable_openai(exc: Exception) -> bool:
    """Return True for transient OpenAI-compatible errors worth retrying.

    Used by both :class:`OpenAIProvider` and :class:`OpenAICompatibleProvider`.
    An error whose body says ``retryable: false`` (the AI Gateway's 502 for an
    upstream 4xx), or whose headers say ``x-should-retry: false`` (a wait past
    what the SDK honours), is never retried.
    """
    if marked_not_retryable(getattr(exc, "body", None)):
        return False
    if told_not_to_retry(exc):
        return False
    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.InternalServerError):
        return True
    if isinstance(exc, openai.APIConnectionError):
        return True
    return isinstance(exc, openai.APIStatusError) and exc.status_code in (502, 503, 529)


# Model families whose native reasoning_effort range includes ``max``. ``max``
# originated as Anthropic's top tier; elsewhere it is remapped to ``xhigh``,
# the top OpenAI tier, or the model's top reasoning tier would be refused.
MAX_EFFORT_NATIVE_PREFIXES = ("gpt-6", "gpt-5.6")


def supports_max_reasoning_effort(model: str | None) -> bool:
    """Return True if *model* natively accepts ``reasoning_effort='max'``.

    The id is normalized first (``_normalize_family_key``) so gateway/vendor
    prefixed ids reaching the shared OpenAI-compatible base class — e.g.
    ``openai/gpt-6-astra`` via OpenRouter or a LiteLLM alias — match the bare
    family prefixes instead of being silently downgraded to ``xhigh``.
    """
    if not model:
        return False
    key = _normalize_family_key(model)
    return any(key.startswith(p) for p in MAX_EFFORT_NATIVE_PREFIXES)


@functools.lru_cache(maxsize=64)
def _log_effort_replaced_by_none(model: str | None, effort: str) -> None:
    # Cached: once per (model, effort), not once per tool iteration.
    logger.info("reasoning_effort %s replaced by none on %r: it refuses an effort with tools", effort, model)


def normalize_openai_reasoning_effort(
    effort: str | None, model: str | None = None, *, has_tools: bool = False
) -> str | None:
    """Return the ``reasoning_effort`` to send *model* for *effort*, ``None`` for none.

    A level the model refuses is an HTTP 400, so a model whose levels are
    declared (``model_capabilities.MODEL_PARAMETER_RULES``) gets the nearest
    one at or below *effort* - none at all on a model that takes no effort
    (``gpt-4o``, ``grok-4.3``, ``mistral-large``) or has no lower level. A value
    outside the platform's levels (a proxy client's ``minimal`` / ``none``)
    passes through. ``max`` becomes ``xhigh`` except on the families that
    accept it natively (:data:`MAX_EFFORT_NATIVE_PREFIXES`). With *has_tools*,
    a model that refuses an effort next to tools (``gpt-6``) gets ``none``,
    the one value OpenAI accepts there, even when *effort* is unset: left
    out, the model's default effort is refused the same way.
    """
    if has_tools and refuses_effort_with_tools(model):
        if effort in EFFORT_LEVELS:
            _log_effort_replaced_by_none(model, effort)
        return "none"
    if not effort or takes_no_effort(model):
        return None
    if effort not in EFFORT_LEVELS:
        if refuses_effort_none(model) and effort in _EFFORT_OFF_VALUES:
            return lowest_effort(model)
        return effort
    supported = supported_effort(model, effort)
    if supported == "max" and not supports_max_reasoning_effort(model):
        return "xhigh"
    return supported


# A caller's "reasoning off" values: a model that refuses them gets its lowest level.
_EFFORT_OFF_VALUES = frozenset({"none", "minimal"})
_SUPPORTED_VALUES = re.compile(r"[Ss]upported values are:?([^.]*)")
_QUOTED_VALUE = re.compile(r"'([a-z]+)'")
_NO_RETRY = object()
_TEMPERATURE_REFUSAL = re.compile(r"Unsupported (?:parameter|value): 'temperature'")


# The AI Gateway's 502 for an upstream 4xx: "... upstream returned status 400 Bad Request",
# then ": <the provider's message>" since XTM-One-Platform/ai-gateway#348.
_GATEWAY_UPSTREAM_REFUSAL = re.compile(r"upstream returned status (4\d\d)[^:]*(?:: (.+))?", re.DOTALL)


def gateway_upstream_refusal(exc: BaseException) -> tuple[int, str | None] | None:
    """The upstream status and the provider's message of the gateway's 502 for an upstream 4xx.

    The message is ``None`` from an older gateway or one that withheld it; ``None`` for any other error.
    """
    if not (isinstance(exc, openai.APIStatusError) and exc.status_code == 502 and marked_not_retryable(exc.body)):
        return None
    match = _GATEWAY_UPSTREAM_REFUSAL.search(_error_message(exc))
    return (int(match.group(1)), match.group(2)) if match else None


def _refusal_message(exc: openai.APIStatusError) -> str | None:
    """What a refused request says: a 400's message, or the provider's 400 behind the gateway."""
    if isinstance(exc, openai.BadRequestError):
        return str(getattr(exc, "message", "") or exc)
    upstream = gateway_upstream_refusal(exc)
    return upstream[1] if upstream and upstream[0] == 400 else None


def _effort_after_refusal(exc: openai.APIStatusError, sent: str | None) -> object:
    """The ``reasoning_effort`` to retry with after *exc*, ``None`` to omit it, or ``_NO_RETRY``.

    Only a refusal (``_refusal_message``) that names ``reasoning_effort`` is answered: the value the
    error lists that is nearest below *sent* (the lowest when *sent* is not a
    platform level), ``none`` when the error asks for it, else no parameter.
    """
    message = _refusal_message(exc)
    if message is None:
        return _NO_RETRY
    if getattr(exc, "param", None) != "reasoning_effort" and "reasoning_effort" not in message:
        return _NO_RETRY
    if "reasoning_effort to 'none'" in message:
        return "none" if sent != "none" else None
    if sent is None:
        return _NO_RETRY
    listed = _SUPPORTED_VALUES.search(message)
    candidates = [
        value for value in (_QUOTED_VALUE.findall(listed.group(1)) if listed else ())
        if value in EFFORT_LEVELS and value != sent
    ]
    if not candidates:
        return None
    if sent in EFFORT_LEVELS:
        below = [v for v in candidates if EFFORT_LEVELS.index(v) < EFFORT_LEVELS.index(sent)]
        if below:
            return max(below, key=EFFORT_LEVELS.index)
    return min(candidates, key=EFFORT_LEVELS.index)


@functools.lru_cache(maxsize=64)
def _log_effort_refused(model: str | None, sent: str | None, retried: str | None) -> None:
    # Cached: once per (model, refused value), not once per request.
    logger.warning(
        "reasoning_effort %s refused by %r: retried with %s", sent, model, retried or "no reasoning_effort"
    )


def _refuses_temperature(exc: openai.APIStatusError) -> bool:
    """Whether *exc* is the refusal a model answers a ``temperature`` it does not take with."""
    message = _refusal_message(exc)
    if message is None:
        return False
    # Behind the gateway the 400's ``param`` is lost: its message names the parameter or the value.
    return getattr(exc, "param", None) == "temperature" or _TEMPERATURE_REFUSAL.search(message) is not None


@functools.lru_cache(maxsize=64)
def _log_temperature_refused(model: str | None) -> None:
    # Cached: once per model, not once per request.
    logger.warning("temperature refused by %r: retried without temperature", model)


async def create_without_refused_temperature[T](
    create: Callable[[dict[str, Any]], Awaitable[T]], params: dict[str, Any], model: str | None
) -> T:
    """Run ``create(params)``; on a refusal of ``temperature``, retry once without it.

    The capability table (``omit_temperature``) is the first line; this keeps
    a model the table does not flag yet on the same leg, on chat completions
    and the Responses API alike, instead of failing over.
    """
    try:
        return await create(params)
    except openai.APIStatusError as exc:
        if "temperature" not in params or not _refuses_temperature(exc):
            raise
        _log_temperature_refused(model)
        return await create({k: v for k, v in params.items() if k != "temperature"})


class Endpoint(NamedTuple):
    """Where a chat-completions request goes: base URL and a hash of its key, never the key."""

    url: str
    credential: str


def endpoint_of(client: object, credential: str | None = None) -> Endpoint | None:
    """*client*'s endpoint; *credential* when its ``api_key`` is not what authenticates it."""
    base_url = getattr(client, "base_url", None)
    if not base_url:
        return None
    key = credential if credential is not None else str(getattr(client, "api_key", "") or "")
    return Endpoint(str(base_url), hashlib.sha256(key.encode()).hexdigest()[:16])


# Stop-gap until XTM-One-Platform/ai-gateway#354: the gateway cannot take these
# requests, and an OpenAI-compatible endpoint is not known to serve /v1/responses.
# (endpoint, model) -> time until which a request with tools fails unsent.
_TOOLS_REFUSED_UNTIL: dict[tuple[Endpoint, str | None], float] = {}
#: Long enough to spare the doomed pair on every tool call, short enough to
#: pick up an endpoint that starts taking them without a restart.
_TOOLS_REFUSAL_TTL_SECONDS = 600.0
_monotonic = time.monotonic


def forget_tools_refusals() -> None:
    _TOOLS_REFUSED_UNTIL.clear()


def _remember_tools_refused(key: tuple[Endpoint, str | None]) -> None:
    # Expired entries go here, so the map holds only the last TTL's refusals.
    now = _monotonic()
    for expired in [k for k, until in _TOOLS_REFUSED_UNTIL.items() if until <= now]:
        del _TOOLS_REFUSED_UNTIL[expired]
    _TOOLS_REFUSED_UNTIL[key] = now + _TOOLS_REFUSAL_TTL_SECONDS


async def create_with_effort_retry[T](
    create: Callable[[dict[str, Any]], Awaitable[T]],
    params: dict[str, Any],
    model: str | None,
    *,
    endpoint: Endpoint | None = None,
) -> T:
    """Run ``create(params)``; on a refusal of ``reasoning_effort``, retry once corrected.

    The capability table (``normalize_openai_reasoning_effort``) is the first
    line; this answers a model the table does not know yet, or one whose
    accepted values moved, on the same leg instead of failing over. A refused
    ``temperature`` is retried once without it on each request
    (``create_without_refused_temperature``). With *endpoint*, a request with
    tools that ended on ``function_tools_unsupported`` there fails unsent for
    :data:`_TOOLS_REFUSAL_TTL_SECONDS`, even behind a gateway that balances
    the model over upstreams of which only some refuse: accepted, as one
    refusal costs two requests on every tool call otherwise.
    """
    refusal_key = (endpoint, model) if endpoint and params.get("tools") else None
    if refusal_key and _TOOLS_REFUSED_UNTIL.get(refusal_key, 0.0) > _monotonic():
        request = httpx.Request("POST", redact_url(f"{endpoint.url.split('?')[0].rstrip('/')}/chat/completions"))
        raise _function_tools_unsupported(model, request, httpx.Headers())

    async def send(request: dict[str, Any]) -> T:
        return await create_without_refused_temperature(create, request, model)

    try:
        return await send(params)
    except openai.APIStatusError as exc:
        sent = params.get("reasoning_effort")
        retried = _effort_after_refusal(exc, sent)
        if retried is _NO_RETRY:
            raise
        corrected = {k: v for k, v in params.items() if k != "reasoning_effort"}
        if retried is not None:
            corrected["reasoning_effort"] = retried
        _log_effort_refused(model, sent, retried)
        try:
            return await send(corrected)
        except openai.APIStatusError as second:
            if retried == "none" and _effort_after_refusal(second, "none") is not _NO_RETRY:
                if refusal_key:
                    _remember_tools_refused(refusal_key)
                    logger.warning(
                        "%s refuses function tools on chat completions at %s: failing fast for %ds",
                        model, redact_url(endpoint.url), _TOOLS_REFUSAL_TTL_SECONDS,
                    )
                raise function_tools_unsupported(second, model) from second
            raise


#: Code of the leg cause a chat-completions request with tools ends on when
#: the model asks for ``none`` next to tools and refuses ``none``.
FUNCTION_TOOLS_UNSUPPORTED = "function_tools_unsupported"


def function_tools_unsupported(exc: openai.APIStatusError, model: str | None) -> openai.BadRequestError:
    """The one 400 a model with no valid chat-completions request with tools ends on.

    Its tools refusal asks for ``reasoning_effort: none`` and ``none`` is
    refused too: no third request can succeed on this endpoint, so the leg
    fails once, named, instead of on the raw second refusal
    (``model_capabilities.responses_api_with_tools`` routes such a model to
    the Responses API).
    """
    # A 400 even after a gateway 502: the leg ends on the request, not on the gateway.
    return _function_tools_unsupported(model, exc.request, exc.response.headers)


def _function_tools_unsupported(
    model: str | None, request: httpx.Request, headers: httpx.Headers
) -> openai.BadRequestError:
    message = f"function tools unsupported on chat completions for {model or 'this model'}"
    body = {"message": message, "param": "reasoning_effort", "code": FUNCTION_TOOLS_UNSUPPORTED, "retryable": False}
    response = httpx.Response(400, headers=headers, request=request)
    return openai.BadRequestError(message, response=response, body=body)


def _coerce_usage_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value < 0 or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = float(stripped)
        except ValueError:
            return None
        if parsed < 0 or not parsed.is_integer():
            return None
        return int(parsed)
    return None


def _usage_details_to_dict(value: object) -> dict[str, int]:
    if value is None:
        return {}

    payload: Mapping[object, object] | None = None
    if isinstance(value, Mapping):
        payload = value
    elif hasattr(value, "model_dump"):
        dumped = value.model_dump()  # type: ignore[call-arg]
        if isinstance(dumped, Mapping):
            payload = dumped
    elif hasattr(value, "dict"):
        dumped = value.dict()  # type: ignore[call-arg]
        if isinstance(dumped, Mapping):
            payload = dumped
    elif hasattr(value, "__dict__"):
        attrs = getattr(value, "__dict__", None)
        if isinstance(attrs, Mapping):
            payload = attrs

    if payload is None:
        return {}

    out: dict[str, int] = {}
    for key, raw in payload.items():
        if not isinstance(key, str):
            continue
        parsed = _coerce_usage_int(raw)
        if parsed is None:
            continue
        out[key] = parsed
    return out


def extract_openai_usage(usage_obj: object) -> dict:
    """Normalize an OpenAI usage object to the provider-standard usage dict.

    Returns the same ``{"prompt_tokens", "completion_tokens"}`` shape the
    non-streaming ``chat()`` path produces, so streamed turns are costed
    identically in Langfuse.  Returns ``{}`` when no usage object is
    present (e.g. a server that ignored ``stream_options``) so callers can
    keep treating "no usage" as falsy.  All access is defensive.
    """
    if usage_obj is None:
        return {}
    prompt = (
        _coerce_usage_int(getattr(usage_obj, "prompt_tokens", None))
        or _coerce_usage_int(getattr(usage_obj, "input_tokens", None))
        or 0
    )
    completion = (
        _coerce_usage_int(getattr(usage_obj, "completion_tokens", None))
        or _coerce_usage_int(getattr(usage_obj, "output_tokens", None))
        or 0
    )
    total = _coerce_usage_int(getattr(usage_obj, "total_tokens", None))

    prompt_details = _usage_details_to_dict(
        getattr(usage_obj, "prompt_tokens_details", None)
    )
    completion_details = _usage_details_to_dict(
        getattr(usage_obj, "completion_tokens_details", None)
    )
    input_details = _usage_details_to_dict(
        getattr(usage_obj, "input_tokens_details", None)
    )
    output_details = _usage_details_to_dict(
        getattr(usage_obj, "output_tokens_details", None)
    )

    if not (
        prompt
        or completion
        or total
        or prompt_details
        or completion_details
        or input_details
        or output_details
    ):
        return {}

    result: dict[str, object] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }
    if total is not None:
        result["total_tokens"] = total
    if prompt_details:
        result["prompt_tokens_details"] = prompt_details
    if completion_details:
        result["completion_tokens_details"] = completion_details
    if input_details:
        result["input_tokens_details"] = input_details
    if output_details:
        result["output_tokens_details"] = output_details

    return result


def extract_openai_response_usage(usage_obj: object) -> dict:
    """Usage dict of a NON-streaming chat completion.

    The ``prompt_tokens`` / ``completion_tokens`` pair this path has always
    returned, plus ``total_tokens`` when the server reports it, as the
    streaming path (:func:`extract_openai_usage`) already does. Without it the
    run budget cannot see tokens only the total accounts for: a server
    that fills in nothing else, or xAI, whose total carries the reasoning
    tokens its ``completion_tokens`` leaves out.

    ``prompt_tokens_details`` is carried like the streaming path does:
    its ``cached_tokens`` are inside ``prompt_tokens`` and priced at the
    cache-read rate, so a streamed and a non-streamed call cost the same. The
    completion details stay out - they are inside ``completion_tokens`` and
    priced at the same output rate either way.
    """
    if not usage_obj:
        return {"prompt_tokens": 0, "completion_tokens": 0}
    usage: dict[str, object] = {
        "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
        "completion_tokens": getattr(usage_obj, "completion_tokens", None),
    }
    total = _coerce_usage_int(getattr(usage_obj, "total_tokens", None))
    if total is not None:
        usage["total_tokens"] = total
    prompt_details = _usage_details_to_dict(getattr(usage_obj, "prompt_tokens_details", None))
    if prompt_details:
        usage["prompt_tokens_details"] = prompt_details
    return usage


def _refused(exc: Exception) -> bool:
    """The request itself was refused: a 400 / 413, or the gateway's 502 for an
    upstream 4xx (``retryable: false``). Not a throttle or an outage."""
    if not isinstance(exc, openai.APIStatusError):
        return False
    return exc.status_code in (400, 413) or (exc.status_code == 502 and marked_not_retryable(exc.body))


def _refuses_replay(exc: Exception) -> bool:
    """A refusal the replayed thinking blocks may have caused, the gateway's read as the upstream
    status: a 400 must say so, a 413 cannot be told apart, another 4xx never is."""
    if not _refused(exc):
        return False
    status, message = gateway_upstream_refusal(exc) or (exc.status_code, _error_message(exc))
    if status != 400:
        # 502: an upstream refusal the gateway named no status for.
        return status in (413, 502)
    if message is None:
        return True
    return (
        refuses_replayed_blocks(message) or refuses_replayed_signatures(message) or says_prompt_too_long(message)
    )


async def _create_completion(
    client, params: dict, model: str | None, replay: BlocksReplay | None, endpoint: Endpoint | None = None
):
    """``chat.completions.create`` with the effort / temperature retries, sent
    once more without the replayed thinking blocks when a request carrying them
    is refused."""

    def send():
        return create_with_effort_retry(
            lambda p: client.chat.completions.create(**p), params, model, endpoint=endpoint or endpoint_of(client)
        )

    try:
        return await send()
    except openai.APIStatusError as exc:
        if replay is None or not replay.sent or not _refuses_replay(exc):
            raise
        status, message = gateway_upstream_refusal(exc) or (exc.status_code, _error_message(exc))
        too_big = status == 413 or (message is not None and says_prompt_too_long(message))
        # Nobody said why (an older gateway, a bare 502): the signatures may be the cause.
        untold = message is None or status == 502
        reason = message or _error_message(exc)
        retry = replay.retry_without(reason, params["messages"], shed_signatures=too_big or untold)
        if retry is None:
            raise
        params["messages"] = retry
    try:
        return await send()
    except openai.APIStatusError as exc:
        if _refused(exc):
            # Refused without them too: the blocks were not the cause.
            replay.restore()
        raise


def _error_message(exc: openai.APIStatusError) -> str:
    """The body's ``error.message``, else the status: never the SDK's rendering,
    which carries the whole body (a proxy page can echo the request)."""
    # The SDK hands over the ``error`` envelope's content, or the whole body.
    message = gateway_error_envelope(exc.body).get("message")
    return message if isinstance(message, str) and message else f"HTTP {exc.status_code}"


def openai_reasoning_text(obj: object) -> str | None:
    """Reasoning prose carried by a streamed delta or a response message.

    OpenAI-compatible servers disagree on the field: ``reasoning_content``
    (DeepSeek, LiteLLM, vLLM) or ``reasoning`` (OpenRouter, recent vLLM and
    Ollama builds) - reading only the first left the chat reasoning window
    empty behind OpenRouter. One field is read, never both, so a
    server that sends the same text under both names is not doubled.
    """
    for field in ("reasoning_content", "reasoning"):
        value = getattr(obj, field, None)
        if isinstance(value, str) and value:
            return value
    return None


def _chunk_field(chunk: object, name: str) -> object:
    if isinstance(chunk, Mapping):
        return chunk.get(name)
    return getattr(chunk, name, None)


def _chunk_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            part if isinstance(part, str) else _chunk_text(_chunk_field(part, "text"))
            for part in value
        )
    return ""


def openai_content_parts(content: object) -> tuple[str, str]:
    """``(answer text, reasoning text)`` carried by a message or delta ``content``.

    OpenAI-compatible servers send a string, except Mistral once
    ``reasoning_effort`` turns reasoning on: a list of ``text`` chunks (the
    answer) and ``thinking`` chunks (the reasoning, itself a list of text
    chunks). Read as a string, that list raised on the stream and put the
    reasoning in the stored answer.
    """
    if isinstance(content, str):
        return content, ""
    if not isinstance(content, list):
        return "", ""
    text: list[str] = []
    thinking: list[str] = []
    for chunk in content:
        kind = _chunk_field(chunk, "type")
        if kind == "text":
            text.append(_chunk_text(_chunk_field(chunk, "text")))
        elif kind == "thinking":
            thinking.append(_chunk_text(_chunk_field(chunk, "thinking")))
    return "".join(text), "".join(thinking)


def content_filter_finish(choice: object) -> object | None:
    """*choice* when it ended on ``content_filter``, ``None`` otherwise.

    The one place that finish reason is named: a stream keeps the choice with
    ``filtered = content_filter_finish(choice) or filtered`` and raises
    :func:`declined_by_filter` once its usage is in.
    """
    return choice if getattr(choice, "finish_reason", None) == "content_filter" else None


def declined_by_filter(choice: object, model: str | None, usage: dict) -> ModelDeclinedError | None:
    """The decline a ``content_filter`` finish raises, ``None`` for any other.

    The gateway names its category in the choice's ``stop_details``; a
    provider's own filter names none, so it never fails over.
    """
    if content_filter_finish(choice) is None:
        return None
    return declined_error(model, getattr(choice, "stop_details", None), usage)


def declared_model_types(types: object, type_: object) -> tuple[str, ...]:
    """The types a ``/models`` entry declares: its ``types``, else its ``type``, else none.

    ``types`` outranks ``type``: the Filigran AI Gateway derives ``type`` from
    it as the primary type alone, so reading ``type`` first would drop a second
    type. Unknown names are kept and match no capability.
    """
    if isinstance(types, list):
        texts = tuple(dict.fromkeys(t for t in types if isinstance(t, str) and t))
        if texts:
            return texts
    return (type_,) if isinstance(type_, str) and type_ else ()


def drop_claude_assistant_tail(oai_messages: list[dict], model: str | None) -> list[dict]:
    """Drop the trailing assistant turns when *model* is a Claude model.

    Claude rejects assistant prefill ("must end with a user message") on any
    OpenAI-compatible route too, so this mirrors the Anthropic provider.
    Trailing system turns are looked past and kept: the route hoists them out,
    so an assistant turn before them still ends the request.
    """
    if not model or not _normalize_family_key(model).startswith("claude"):
        return oai_messages
    start = len(oai_messages)
    while start and oai_messages[start - 1].get("role") in ("assistant", "system"):
        start -= 1
    tail = oai_messages[start:]
    kept = [m for m in tail if m.get("role") == "system"]
    if len(kept) == len(tail):
        return oai_messages
    logger.info(
        "Stripped %d trailing assistant message(s) for Claude model %s (no prefill)",
        len(tail) - len(kept),
        model,
    )
    return oai_messages[:start] + kept


def openai_function_tools(
    tools: list[ToolDefinition] | None, model: str | None, *, upstream: str | None = None
) -> list[dict] | None:
    """*tools* as OpenAI function tools. Routes like the gateway pass the schema
    through, so when Anthropic or Gemini serves the model (*upstream*, else Anthropic
    for a Claude name) they take that provider's own wire form: Claude rejects
    top-level ``allOf``/``anyOf``/``oneOf`` and duplicate names, Gemini type unions,
    empty enums and most JSON Schema keys."""
    if not tools:
        return None
    if upstream is None and is_claude_model(model):
        upstream = "anthropic"
    # Lazy imports: base.py imports this module, both providers import base.py.
    if upstream == "anthropic":
        from app.llm.anthropic_provider import _wire_tools

        wire = [(w["name"], w["description"], w["input_schema"]) for w in _wire_tools(tools)]
    elif upstream == "gemini":
        from app.llm.gemini_provider import gemini_wire_tools

        wire = gemini_wire_tools(tools)
    else:
        wire = [(t.name, t.description, t.input_schema) for t in tools]
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                **({"parameters": schema} if schema is not None else {}),
            },
        }
        for name, description, schema in wire
    ]


# ---------------------------------------------------------------------------
# Shared OpenAI-format streaming-with-tools consumer
# ---------------------------------------------------------------------------
# Used by both OpenAIProvider and OpenAICompatibleProvider (and subclasses)
# to avoid duplicating the streaming accumulation loop.


async def consume_openai_stream_with_tools(
    client,
    *,
    model: str,
    oai_messages: list[dict],
    tools: list[ToolDefinition] | None = None,
    on_token: Callable[[str], Awaitable[None]] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    include_usage: bool = True,
    shape_request: Callable[[dict], Awaitable[None]] | None = None,
    replay: BlocksReplay | None = None,
    tools_upstream: str | None = None,
    endpoint: Endpoint | None = None,
    **kwargs,
) -> ChatResponse:
    """Consume an OpenAI-format streaming response, handling both text and tool calls.

    Builds the request params, opens the stream, and accumulates chunks into
    a :class:`ChatResponse`.  Text tokens are forwarded to *on_token* in
    real-time when no tool calls are being produced.

    When *include_usage* is True (the default) the request asks OpenAI to
    emit a final usage chunk via ``stream_options={"include_usage": True}``
    — without it the streaming API never reports token counts and the
    resulting :class:`ChatResponse` would carry empty usage, leaving every
    streamed turn with zero tokens / zero cost in Langfuse.  Providers whose
    OpenAI-compatible endpoint rejects ``stream_options`` can pass
    ``include_usage=False``.

    Transient errors (429, 5xx, connection failures) are retried via
    :func:`retry_llm_call` — but only while nothing has been delivered to
    the caller.  Once a token or thinking chunk has been forwarded through
    *on_token* / *on_thinking*, replaying the stream would duplicate output,
    so any later error propagates immediately.

    With a *replay* (:mod:`app.llm.openai_thinking_blocks`), a refusal of its
    thinking blocks is retried once without them, and the response's own
    blocks and thought signatures are kept on ``_provider_extra``.

    *tools_upstream* is the provider serving *model* (:func:`openai_function_tools`).
    """
    params: dict = {"model": model, "messages": oai_messages, "stream": True}
    if include_usage:
        params["stream_options"] = {"include_usage": True}
    params.update(
        build_openai_generation_params(
            model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    )
    if reasoning_effort := normalize_openai_reasoning_effort(
        kwargs.get("reasoning_effort"), model, has_tools=bool(tools)
    ):
        params["reasoning_effort"] = reasoning_effort
    if tools:
        params["tools"] = openai_function_tools(tools, model, upstream=tools_upstream)
    if shape_request is not None:
        await shape_request(params)

    on_thinking = kwargs.get("on_thinking")
    usage_sink = usage_sink_of(kwargs)

    # Retry is only safe while no output has escaped to the caller.  The
    # flag flips just before the first ``on_token`` / ``on_thinking``
    # delivery; afterwards the retryable predicate returns False and any
    # error propagates immediately (replaying would duplicate output).
    emitted = False

    async def _stream_once() -> ChatResponse:
        nonlocal emitted

        if usage_sink is not None:
            usage_sink.restart()
        stream = await _create_completion(client, params, model, replay, endpoint)

        content_chunks: list[str] = []
        thinking_chunks: list[str] = []
        thinking_blocks: list[dict] = []
        tool_calls_by_idx: dict[int, dict] = {}
        stop_reason = "end_turn"
        has_tool_calls = False
        usage_obj = None
        filtered_choice = None

        async for chunk in stream:
            # The usage chunk arrives last and carries an empty ``choices``
            # list, so capture it before the ``not chunk.choices`` guard skips
            # the chunk entirely.
            if getattr(chunk, "usage", None) is not None:
                usage_obj = chunk.usage
                if usage_sink is not None:
                    usage_sink.report(extract_openai_usage(usage_obj))
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason is not None:
                if choice.finish_reason == "tool_calls":
                    stop_reason = "tool_use"
                elif choice.finish_reason == "length":
                    stop_reason = "max_tokens"
                else:
                    stop_reason = "end_turn"
                filtered_choice = content_filter_finish(choice) or filtered_choice

            if replay is not None:
                thinking_blocks.extend(blocks_of(delta))
            text, content_thinking = openai_content_parts(delta.content)
            for reasoning in (openai_reasoning_text(delta), content_thinking):
                if reasoning:
                    thinking_chunks.append(reasoning)
                    if on_thinking:
                        emitted = True
                        await on_thinking(reasoning)

            if delta.tool_calls:
                has_tool_calls = True
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_by_idx:
                        tool_calls_by_idx[idx] = {"id": "", "name": "", "arguments": [], "signature": None}
                    entry = tool_calls_by_idx[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if signature := thought_signature_of(tc_delta):
                        entry["signature"] = signature
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"].append(tc_delta.function.arguments)

            if text:
                content_chunks.append(text)
                if not has_tool_calls and on_token:
                    emitted = True
                    await on_token(text)

        usage = extract_openai_usage(usage_obj)
        if declined := declined_by_filter(filtered_choice, model, usage):
            raise declined

        parsed_tool_calls: list[ToolCall] | None = None
        if tool_calls_by_idx:
            parsed_tool_calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["name"],
                    arguments=parse_tool_arguments(
                        "".join(tc["arguments"]), tool_name=tc["name"]
                    ),
                )
                for idx, tc in sorted(tool_calls_by_idx.items())
            ]

        return ChatResponse(
            content="".join(content_chunks),
            model=model,
            usage=usage,
            tool_calls=parsed_tool_calls,
            stop_reason=stop_reason,
            thinking="".join(thinking_chunks) if thinking_chunks else None,
            _provider_extra=replay.capture(
                thinking_blocks,
                parsed_tool_calls,
                {tc["id"]: tc["signature"] for tc in tool_calls_by_idx.values() if tc["signature"]},
            )
            if replay
            else None,
        )

    return await retry_llm_call(
        _stream_once,
        is_retryable=lambda exc: not emitted and is_retryable_openai(exc),
    )


class OpenAICompatibleProvider(OpenAISpeechMixin, OpenAIVideosMixin, BaseLLMProvider):
    """Reusable base for any OpenAI-compatible API endpoint.

    Subclasses only need to set class-level attributes and optionally
    override ``list_models()`` for provider-specific model discovery. Video
    generation is the OpenAI Videos API (``app.llm.video``) and speech the
    OpenAI speech API (``app.llm.speech``): an endpoint that serves them
    offers its video and speech models, one that does not lists none. The
    voices are its upstream's, passed through as given.

    Class attributes to override:
        provider_name: str — e.g. ``"openrouter"``
        default_model: str — fallback model when none is specified
        base_url: str — API base URL
    """

    speech_voices_are_fixed = False
    provider_name: str = "openai_compat"
    default_model: str = "gpt-4o"
    base_url: str | None = None  # None → default OpenAI endpoint

    # Whether the endpoint honours ``stream_options={"include_usage": True}``
    # to emit a final usage chunk during streaming.  True for OpenAI and all
    # spec-compliant compatible servers; a subclass whose backend rejects the
    # parameter can set this False to fall back to usage-less streaming.
    supports_stream_usage: bool = True

    # ``response_format`` (json_schema) is supported on streaming chat
    # completions exactly like non-streaming ones, so the agentic loop can
    # forward ``structured_output`` to ``chat_stream()``.
    # Subclasses whose backend rejects the parameter can set this False to
    # fall back to prompt-based SAP enforcement.
    supports_streaming_structured_output: bool = True

    # Whether a response's Claude ``thinking_blocks`` and Gemini thought
    # signatures are kept and sent back within the tool round
    # (:mod:`app.llm.openai_thinking_blocks`).
    replays_thinking_blocks: bool = False

    def __init__(self, api_key: str, **kwargs):
        client_kwargs: dict = {"api_key": api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        client_kwargs.setdefault(
            "timeout", httpx.Timeout(connect=10, read=600, write=30, pool=10)
        )
        client_kwargs.update(kwargs)
        self.client = AsyncOpenAI(**client_kwargs)

    def _reasoning_effort(self, model: str, kwargs: dict) -> str | None:
        """The ``reasoning_effort`` the caller asked for, before normalization."""
        return kwargs.get("reasoning_effort")

    def refusal_endpoint(self) -> Endpoint | None:
        """Where a tools refusal is remembered (``create_with_effort_retry``)."""
        return endpoint_of(self.client)

    def _chat_client(self) -> AsyncOpenAI:
        """The client chat completions are sent with; every such call is wrapped in ``retry_llm_call``."""
        return self.client

    async def _shape_request(self, params: dict, kwargs: dict) -> None:
        """Last edit of a chat-completions request before it is sent; *kwargs* are the caller's."""

    async def _upstream_of(self, model: str) -> str | None:
        """The provider serving *model*, whose wire form its tools take
        (:func:`openai_function_tools`); ``None`` when unknown."""
        return "anthropic" if is_claude_model(model) else None

    async def _build_messages(self, messages: list[ChatMessage], model: str) -> list[dict]:
        oai_messages = await build_openai_messages_async(
            messages, "openai_compat", context_window=self._resolve_context_window(model)
        )
        return drop_claude_assistant_tail(oai_messages, model)

    async def _prepare_messages(
        self, messages: list[ChatMessage], model: str, tools: list[ToolDefinition] | None, kwargs: dict
    ) -> tuple[list[dict], BlocksReplay | None]:
        """The request's messages, checked against the context window, and its
        thinking-block replay when the provider sends blocks back. The blocks
        are dropped for this request when they would not fit, and never sent
        with ``disable_thinking``, as the Anthropic provider does; Gemini's
        signatures still are, as the Gemini provider does."""
        oai_messages = await self._build_messages(messages, model)
        text_chars, image_count, native_file_bytes = self._measure_oai_payload(oai_messages)
        self._check_resolved_overflow(
            text_chars=text_chars, image_count=image_count,
            native_file_bytes=native_file_bytes, tools=tools, model=model,
        )
        if not self.replays_thinking_blocks:
            return oai_messages, None
        replay = BlocksReplay.build(
            oai_messages, messages, self.provider_name, model, blocks=not kwargs.get("disable_thinking")
        )
        limit = self._resolve_context_window(model) * self._OVERFLOW_THRESHOLD
        # The blocks go first; the signatures, small, only when they alone do not fit.
        for signatures in (False, True):
            if not replay.sent:
                break
            estimated = self._estimate_resolved_tokens(
                text_chars=text_chars + replay.chars(), image_count=image_count,
                native_file_bytes=native_file_bytes, tool_chars=self._tool_definition_chars(tools),
            )[0]
            if estimated <= limit:
                break
            if replay.drop(signatures=signatures):
                logger.info(
                    "Dropping replayed %s (model=%s): ~%d tokens with them",
                    "thought signatures" if signatures else "thinking blocks", model, estimated,
                )
        return replay.messages, replay

    # ------------------------------------------------------------------
    # chat (with tool support) — identical to OpenAI provider
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

        oai_messages, replay = await self._prepare_messages(messages, model, tools, kwargs)

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
            self._reasoning_effort(model, kwargs), model, has_tools=bool(tools)
        ):
            params["reasoning_effort"] = reasoning_effort

        # Structured output: native response_format for JSON schema enforcement
        if structured_output := kwargs.get("structured_output"):
            params["response_format"] = structured_output

        if tools:
            params["tools"] = openai_function_tools(tools, model, upstream=await self._upstream_of(model))
        await self._shape_request(params, kwargs)

        response = await retry_llm_call(
            lambda: _create_completion(self._chat_client(), params, model, replay, self.refusal_endpoint()),
            is_retryable=is_retryable_openai,
        )

        choice = response.choices[0]
        message = choice.message
        usage = extract_openai_response_usage(response.usage)
        if declined := declined_by_filter(choice, model, usage):
            raise declined

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
        content, content_thinking = openai_content_parts(message.content)
        reasoning = openai_reasoning_text(message) or content_thinking

        return ChatResponse(
            content=content,
            model=response.model,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            stop_reason=stop_reason,
            thinking=reasoning if reasoning else None,
            _provider_extra=replay.capture(
                blocks_of(message),
                tool_calls,
                {tc.id: sig for tc in message.tool_calls or [] if (sig := thought_signature_of(tc))},
            )
            if replay
            else None,
        )

    # ------------------------------------------------------------------
    # chat_stream
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
        model = model or self.default_model
        usage_sink = usage_sink_of(kwargs)
        if usage_sink is not None:
            usage_sink.restart()

        if tools:
            # ``chat_stream`` is the plain text-streaming path; it cannot emit
            # tool calls (that is ``chat_stream_with_tools``). Log the dropped
            # tools instead of silently discarding them so a mis-routed
            # tool-enabled call is diagnosable.
            logger.debug(
                "chat_stream received %d tool(s) but cannot stream tool calls; "
                "ignoring — use chat_stream_with_tools for tool support (model=%s)",
                len(tools),
                model,
            )

        api_messages, replay = await self._prepare_messages(messages, model, tools, kwargs)

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
        if reasoning_effort := normalize_openai_reasoning_effort(self._reasoning_effort(model, kwargs), model):
            extra_params["reasoning_effort"] = reasoning_effort

        # Structured output: native response_format for JSON schema
        # enforcement — supported on streaming chat completions too.
        # Gated on the capability flag (mirroring ``supports_stream_usage``
        # above) so subclasses that set
        # ``supports_streaming_structured_output = False`` actually opt out
        # even when a caller passes the kwarg to ``chat_stream()`` directly
        # — the agentic loop already gates in ``_streaming_chat``, but the
        # flag must hold for every caller.
        if self.supports_streaming_structured_output and (
            structured_output := kwargs.get("structured_output")
        ):
            extra_params["response_format"] = structured_output
        params = {"model": model, "messages": api_messages, "stream": True, **extra_params}
        await self._shape_request(params, kwargs)

        self.last_stop_reason = None
        self.last_usage = None
        # Retry transient errors on stream establishment only — nothing has
        # been yielded to the caller until the create() call returns.
        stream = await retry_llm_call(
            lambda: _create_completion(self._chat_client(), params, model, replay, self.refusal_endpoint()),
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
            text, content_thinking = openai_content_parts(delta.content)
            if on_thinking:
                for reasoning in (openai_reasoning_text(delta), content_thinking):
                    if reasoning:
                        await on_thinking(reasoning)
            if text:
                yield text

        # Raised after the loop: the usage chunk trails the finish chunk, and
        # a decline is billed.
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

        oai_messages, replay = await self._prepare_messages(messages, model, tools, kwargs)
        return await consume_openai_stream_with_tools(
            self._chat_client(), model=model, oai_messages=oai_messages, tools=tools,
            on_token=on_token, temperature=temperature, max_tokens=max_tokens,
            include_usage=self.supports_stream_usage, replay=replay,
            shape_request=lambda params: self._shape_request(params, kwargs),
            tools_upstream=await self._upstream_of(model) if tools else None,
            endpoint=self.refusal_endpoint(),
            **{**kwargs, "reasoning_effort": self._reasoning_effort(model, kwargs)},
        )

    # ------------------------------------------------------------------
    # embed (override in subclass if supported)
    # ------------------------------------------------------------------

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """Default embedding — override in subclasses with embedding support."""
        raise NotImplementedError(
            f"{self.provider_name} does not support embeddings via this provider."
        )

    # ------------------------------------------------------------------
    # list_models (override in subclass for provider-specific logic)
    # ------------------------------------------------------------------

    async def list_models(self) -> list[str]:
        """Fetch available models from the API."""
        response = await self.client.models.list()
        return sorted([m.id for m in response.data], reverse=True)

    async def _list_model_entries(self) -> list[dict]:
        """The raw entries of the ``/models`` listing, extra fields included."""
        response = await self.client.models.list()
        return [m.model_dump() for m in response.data]

    async def list_model_context_windows(self) -> dict[str, int] | None:
        """The windows the ``/models`` listing reports, under any field it is known by."""
        windows: dict[str, int] = {}
        for entry in await self._list_model_entries():
            model_id = entry.get("id") if isinstance(entry, dict) else None
            if not isinstance(model_id, str) or not model_id:
                continue
            for field in _LISTED_CONTEXT_WINDOW_FIELDS:
                if tokens := reported_context_window(entry.get(field)):
                    windows[model_id] = tokens
                    break
        return windows
