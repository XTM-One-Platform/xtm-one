"""Harness of the opt-in live LLM smoke suite (see ``README.md`` next to it).

Nothing here touches the network on import: the opt-in gate, the key scrubber,
the provider factory (the platform's own ``router.build_provider``), a
pass-through request spy, and the transcripts the tests send - built the way
``app.tools.agent_loop`` builds them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools.base import ToolDefinition

LIVE_FLAG = "LIVE_LLM_SMOKE"

# The env-seeded provider credentials the backend itself reads
# (``LLM_PROVIDER_{PROVIDER}_API_KEY``, ``main._seed_llm_providers_from_env``).
KEY_ENV = {
    "anthropic": "LLM_PROVIDER_ANTHROPIC_API_KEY",
    "gemini": "LLM_PROVIDER_GEMINI_API_KEY",
    "openrouter": "LLM_PROVIDER_OPENROUTER_API_KEY",
    # The speech models of the pronunciation check; OpenAI also hears them back.
    "openai": "LLM_PROVIDER_OPENAI_API_KEY",
    "elevenlabs": "LLM_PROVIDER_ELEVENLABS_API_KEY",
    # A service account key (its JSON, or that JSON base64-encoded) of a project serving Veo.
    "vertex_ai": "LLM_PROVIDER_VERTEX_AI_API_KEY",
}

# Bound on one live interaction, the provider's own 429 / 5xx retries included.
LIVE_CALL_TIMEOUT_SECS = 300


def skip_reason(provider: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Why the live tests of *provider* are skipped, or ``None`` when they run.

    Only ``LIVE_LLM_SMOKE`` set to exactly ``1`` turns the suite on, and a
    provider whose key is not set stays off even then. Evaluated when the test
    modules are collected, so nothing a test loads into the environment later
    can switch it on mid-run.
    """
    env = os.environ if environ is None else environ
    if env.get(LIVE_FLAG) != "1":
        return f"live LLM smoke suite: set {LIVE_FLAG}=1 to run it (real API calls, real cost)"
    if not env.get(KEY_ENV[provider], "").strip():
        return f"live LLM smoke suite: {KEY_ENV[provider]} is not set"
    return None


def live_marks(provider: str) -> list[pytest.MarkDecorator]:
    """The ``pytestmark`` of a live test module: the marker plus the gate."""
    reason = skip_reason(provider)
    return [
        pytest.mark.live_llm,
        pytest.mark.skipif(reason is not None, reason=reason or "enabled"),
    ]


def model_for(env_var: str, default: str) -> str:
    """The model a live test sends to: *default* unless *env_var* overrides it."""
    return os.environ.get(env_var, "").strip() or default


# ---------------------------------------------------------------------------
# Keeping the keys out of every report
# ---------------------------------------------------------------------------

REDACTED = "[REDACTED]"

# Provider key shapes, masked even when nobody registered the value.
_KEY_SHAPES = re.compile(
    r"sk-ant-[\w-]{8,}|sk-or-[\w-]{8,}|sk-proj-[\w-]{8,}|sk_[0-9a-f]{20,}|AIza[\w-]{20,}|AQ\.[\w-]{20,}"
    r"|-----BEGIN PRIVATE KEY-----.+?-----END PRIVATE KEY-----",
    re.DOTALL,
)

_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mask *value* in everything :func:`scrub` cleans from now on."""
    if value and len(value) >= 8:
        _secrets.add(value)


def scrub(text: str) -> str:
    """*text* with every registered secret and every provider key shape masked."""
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return _KEY_SHAPES.sub(REDACTED, text)


def scrub_report(report: pytest.TestReport) -> None:
    """Mask secrets in what pytest prints for *report*: the failure and the captured output."""
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        path, lineno, reason = longrepr
        report.longrepr = (path, lineno, scrub(str(reason)))
    elif longrepr is not None:
        text = str(longrepr)
        clean = scrub(text)
        if clean != text:
            report.longrepr = clean
    report.sections = [(title, scrub(content)) for title, content in report.sections]


def scrubbing_record_factory(
    previous: Callable[..., logging.LogRecord],
) -> Callable[..., logging.LogRecord]:
    """A log record factory masking secrets before any handler formats the record."""

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            return record
        clean = scrub(message)
        if clean != message:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        return record

    return factory


for _key_env in KEY_ENV.values():
    register_secret(os.environ.get(_key_env, "").strip())


# ---------------------------------------------------------------------------
# Providers, built the way the platform builds them
# ---------------------------------------------------------------------------


def build_live_provider(provider: str) -> BaseLLMProvider:
    """The platform's own provider for *provider*, built by ``router.build_provider``.

    From a config row shaped like the one the settings API stores - the key
    Fernet-encrypted - so the decrypt path runs too. Both forms of the key are
    registered with the scrubber: under a known ``SECRET_KEY`` the token is as
    good as the key.
    """
    from app.core.security import encrypt_value
    from app.llm import router

    key = os.environ[KEY_ENV[provider]].strip()
    register_secret(key)
    token = encrypt_value(key)
    register_secret(token)
    return router.build_provider(
        SimpleNamespace(
            id=uuid.uuid4(),
            provider=provider,
            api_key_enc=token,
            default_model=None,
            available_models=None,
            context_windows=None,
            max_output_tokens=None,
        )
    )


async def close_provider(provider: BaseLLMProvider) -> None:
    """Close the SDK client of *provider* and its connection pool."""
    client = getattr(provider, "client", None)
    aio = getattr(client, "aio", None)
    close = getattr(aio, "aclose", None) if aio is not None else getattr(client, "close", None)
    if close is not None:
        with contextlib.suppress(Exception):
            await close()


# ---------------------------------------------------------------------------
# Calling the API
# ---------------------------------------------------------------------------


def _api_error_types() -> tuple[type[BaseException], ...]:
    import anthropic
    import httpx
    import openai
    from google.genai import errors as genai_errors

    return (anthropic.APIError, openai.APIError, genai_errors.APIError, httpx.HTTPError)


def _status_of(exc: BaseException) -> object:
    return getattr(exc, "status_code", None) or getattr(exc, "code", None)


def describe_api_error(exc: BaseException) -> str:
    """``Class (status=..., type=...): message`` of a provider error - never its request."""
    api_type = getattr(exc, "status", None)
    message = None
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body["error"] if isinstance(body.get("error"), dict) else body
        api_type = error.get("type") or error.get("status") or api_type
        message = error.get("message")
    message = message or getattr(exc, "message", None) or str(exc) or repr(exc)
    return scrub(f"{type(exc).__name__} (status={_status_of(exc)}, type={api_type}): {message}")


async def call_api[T](awaitable: Awaitable[T], *, no_access_skip: str | None = None) -> T:
    """Await one live interaction, bounded in time, a provider error reported as its API error.

    The failure carries the error's class, status, API error type and message,
    never the request - whose headers hold the key. *no_access_skip* turns a
    403 / 404 (a key without access to the model) into a skip with that reason.
    """
    try:
        async with asyncio.timeout(LIVE_CALL_TIMEOUT_SECS):
            return await awaitable
    except TimeoutError:
        pytest.fail(f"no answer within {LIVE_CALL_TIMEOUT_SECS}s", pytrace=False)
    except _api_error_types() as exc:
        if no_access_skip and _status_of(exc) in (403, 404):
            pytest.skip(f"{no_access_skip}: {describe_api_error(exc)}")
        pytest.fail(describe_api_error(exc), pytrace=False)


# ---------------------------------------------------------------------------
# Seeing what the platform sent
# ---------------------------------------------------------------------------


class RequestSpy:
    """Record the keyword arguments of every call to ``owner.name``, then pass it through.

    The SDK client holds the key, not the call arguments, so a recorded call is
    the request body the platform built and nothing secret.
    """

    def __init__(self, owner: object, name: str) -> None:
        self.calls: list[dict[str, Any]] = []
        original = getattr(owner, name)

        def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(dict(kwargs))
            return original(*args, **kwargs)

        setattr(owner, name, spy)


class _DryRunError(Exception):
    """Raised by :func:`capture_request` in place of the network call."""


async def capture_request(owner: object, name: str, call: Callable[[], Awaitable[object]]) -> dict[str, Any]:
    """The keyword arguments *call* would send through ``owner.name`` - without sending them."""
    captured: dict[str, Any] = {}

    def refuse(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise _DryRunError

    had_own = name in vars(owner)
    original = getattr(owner, name)
    setattr(owner, name, refuse)
    try:
        with contextlib.suppress(_DryRunError):
            await call()
    finally:
        if had_own:
            setattr(owner, name, original)
        else:
            delattr(owner, name)
    return captured


# ---------------------------------------------------------------------------
# What the tests send
# ---------------------------------------------------------------------------


class ReasoningCollector:
    """An ``on_thinking`` consumer that reads the text (``base.reads_reasoning`` is true)."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def __call__(self, text: str) -> None:
        self.chunks.append(text)

    @property
    def text(self) -> str:
        return "".join(self.chunks)


# Enough steps that a model thinking adaptively does think, small enough to stay cheap.
REASONING_PROMPT = (
    "How many positive integers below 200 are divisible by 6 or by 15, but not by 10? "
    "Work it out, then reply with only the number."
)
# 33 multiples of 6 + 13 of 15 - 6 of 30 = 40, minus the 6 multiples of 30 (the only ones divisible by 10).
REASONING_ANSWER = "34"

# Shortest reasoning passage checked for in an answer: every answer the suite asks
# for is a bare number, so a passage this long cannot be there by coincidence.
_MIN_LEAKED_PASSAGE = 12


async def stream_turn(
    provider: BaseLLMProvider,
    *,
    model: str,
    max_tokens: int,
    on_thinking: ReasoningCollector,
) -> str:
    """One streamed ``chat_stream`` turn on :data:`REASONING_PROMPT`; returns the answer."""
    chunks: list[str] = []
    async for chunk in provider.chat_stream(
        messages=[ChatMessage(role="user", content=REASONING_PROMPT)],
        model=model,
        max_tokens=max_tokens,
        on_thinking=on_thinking,
    ):
        chunks.append(chunk)
    return "".join(chunks)


def leaked_reasoning(answer: str, reasoning: ReasoningCollector | None) -> list[str]:
    """The passages of *reasoning* - all of it, then line by line - found verbatim in *answer*."""
    if reasoning is None:
        return []
    thought = reasoning.text.strip()
    passages = dict.fromkeys([thought, *(line.strip() for line in thought.splitlines())])
    return [passage for passage in passages if len(passage) >= _MIN_LEAKED_PASSAGE and passage in answer]


def assert_reasoning_kept_out_of(answer: str, reasoning: ReasoningCollector) -> None:
    """The reasoning reached the consumer and no passage of it reached the answer."""
    assert reasoning.text.strip(), "no reasoning text reached on_thinking"
    assert answer.strip(), "the answer was empty"
    leaked = leaked_reasoning(answer, reasoning)
    assert not leaked, f"{len(leaked)} reasoning passage(s) leaked into the answer ({len(answer)} chars)"


def assert_answer_is(answer: str, expected: str) -> None:
    """*expected* stands as a number of its own in *answer*, not as part of a longer one."""
    assert expected in re.findall(r"\d+", answer), f"expected {expected} in the answer: {answer[:200]!r}"


LOOKUP_TOOL = ToolDefinition(
    name="lookup_code",
    description=(
        "Return the numeric code stored under a key, and the key to look up next (next_key is null when there is none)."
    ),
    input_schema={
        "type": "object",
        "properties": {"key": {"type": "string", "description": "The key to look up."}},
        "required": ["key"],
    },
)

# Each key is only named by the previous result, so the model has to call the
# tool once per round, and only the tool results can give it the sum.
_FIRST_KEY = "alpha"
_SECOND_KEY = "k7q-zeta"
_CODES: dict[str, tuple[int, str | None]] = {
    _FIRST_KEY: (417, _SECOND_KEY),
    _SECOND_KEY: (286, None),
}
TOOL_LOOP_ANSWER = "703"
TOOL_LOOP_SYSTEM = "You are a terse assistant driven by an automated test. Follow the instructions exactly."
TOOL_LOOP_PROMPT = (
    f"Use the lookup_code tool to look up the key '{_FIRST_KEY}'. Its result names the next key: look that "
    "key up too, and keep going until a result has no next key. Then reply with only the sum of "
    "the codes you received, as digits."
)


def _lookup_key(arguments: Mapping[str, Any]) -> str:
    return str(arguments.get("key", "")).strip().lower()


def run_lookup(arguments: Mapping[str, Any]) -> str:
    """The fake tool: the code stored under ``arguments["key"]`` and the next key, as JSON."""
    key = _lookup_key(arguments)
    if key not in _CODES:
        return json.dumps({"error": f"unknown key: {key!r}"})
    code, next_key = _CODES[key]
    return json.dumps({"key": key, "code": code, "next_key": next_key})


def append_assistant_turn(messages: list[ChatMessage], response: ChatResponse) -> None:
    """Append *response* to the transcript the way ``app.tools.agent_loop`` does.

    Content and tool calls, plus ``_provider_extra`` (Gemini's raw parts, thought
    signatures included). Keep it in step with the loop: a field the loop starts
    carrying onto the assistant message has to be carried here too, or the suite
    stops sending what production sends.
    """
    message = ChatMessage(role="assistant", content=response.content or "", tool_calls=response.tool_calls)
    if response._provider_extra:
        message._provider_extra = response._provider_extra
    messages.append(message)


@dataclass
class ToolLoopResult:
    """Every response of one tool loop, the answer last."""

    responses: list[ChatResponse] = field(default_factory=list)

    @property
    def final(self) -> ChatResponse:
        return self.responses[-1]

    @property
    def keys_per_round(self) -> list[list[str]]:
        """The keys looked up in each tool-calling round, in order."""
        return [
            [_lookup_key(call.arguments) for call in response.tool_calls]
            for response in self.responses
            if response.tool_calls
        ]


async def run_tool_loop(
    provider: BaseLLMProvider,
    *,
    model: str,
    on_thinking: ReasoningCollector | None,
    max_tokens: int,
    max_requests: int = 5,
) -> ToolLoopResult:
    """Drive :data:`LOOKUP_TOOL` through ``chat_stream_with_tools`` until the model answers."""
    messages = [
        ChatMessage(role="system", content=TOOL_LOOP_SYSTEM),
        ChatMessage(role="user", content=TOOL_LOOP_PROMPT),
    ]
    result = ToolLoopResult()

    async def on_token(_text: str) -> None:
        return None

    for _ in range(max_requests):
        response = await provider.chat_stream_with_tools(
            messages=messages,
            tools=[LOOKUP_TOOL],
            on_token=on_token,
            model=model,
            max_tokens=max_tokens,
            on_thinking=on_thinking,
        )
        result.responses.append(response)
        if not response.tool_calls:
            break
        append_assistant_turn(messages, response)
        for call in response.tool_calls:
            messages.append(
                ChatMessage(
                    role="tool",
                    content=run_lookup(call.arguments),
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
    return result


def assert_tool_loop_answered(result: ToolLoopResult) -> None:
    """The first key, then - in a later round - the key only its result named, then the sum.

    The second key cannot be looked up before the first result reached the model,
    and the sum needs both results: the three together prove every tool result
    made it back into the conversation.
    """
    rounds = result.keys_per_round
    assert rounds[:1] == [[_FIRST_KEY]], f"the first round should look up {_FIRST_KEY!r} alone, got {rounds[:1]}"
    assert any(_SECOND_KEY in keys for keys in rounds[1:]), (
        f"{_SECOND_KEY!r} was never looked up after {_FIRST_KEY!r}: {rounds}"
    )
    assert not result.final.tool_calls, f"still calling tools after {len(result.responses)} requests"
    assert_answer_is(result.final.content, TOOL_LOOP_ANSWER)
