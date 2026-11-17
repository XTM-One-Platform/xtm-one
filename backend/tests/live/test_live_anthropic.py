"""Live smoke tests of the Anthropic provider - opt-in, real cost (see ``README.md``).

Claude 5 thinks by default and hides that thinking unless the request asks for
it, which the streaming paths do. The Mythos case sends the lowest-effort
utility request: the form a ``disable_thinking`` call takes on a model that
refuses ``thinking.type.disabled``.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.llm import anthropic_provider
from app.llm.base import ChatMessage
from tests.live.live_llm_support import (
    REASONING_ANSWER,
    ReasoningCollector,
    RequestSpy,
    assert_answer_is,
    assert_reasoning_kept_out_of,
    assert_tool_loop_answered,
    build_live_provider,
    call_api,
    capture_request,
    close_provider,
    leaked_reasoning,
    live_marks,
    model_for,
    run_tool_loop,
    stream_turn,
)

pytestmark = live_marks("anthropic")

CLAUDE_5_MODEL = model_for("LIVE_LLM_ANTHROPIC_MODEL", "claude-sonnet-5")
MYTHOS_MODEL = model_for("LIVE_LLM_ANTHROPIC_MYTHOS_MODEL", "claude-mythos-5")
SONNET_5_5_MODEL = model_for("LIVE_LLM_ANTHROPIC_SONNET_5_5_MODEL", "claude-sonnet-5-5")

# What a streaming call with a reasoning consumer asks a Claude 5 model for.
SUMMARIZED_THINKING = {"type": "adaptive", "display": "summarized"}

# The guarded replay of thinking blocks in a tool loop, once the code has it.
REPLAY_ENV = "ANTHROPIC_THINKING_REPLAY_ENABLED"

UTILITY_SYSTEM = "You are a classification utility. Answer with JSON only."
UTILITY_PROMPT = (
    'Classify the sentiment of "The deployment finished without errors." as positive, '
    'negative or neutral. Reply with {"sentiment": "<label>"} and nothing else.'
)


@pytest.fixture
async def provider():
    llm = build_live_provider("anthropic")
    yield llm
    await close_provider(llm)


async def test_claude_5_streams_its_reasoning_to_the_consumer_only(provider):
    sent = RequestSpy(provider.client.messages, "stream")
    reasoning = ReasoningCollector()

    answer = await call_api(stream_turn(provider, model=CLAUDE_5_MODEL, max_tokens=2048, on_thinking=reasoning))

    assert sent.calls[-1].get("thinking") == SUMMARIZED_THINKING
    assert "temperature" not in sent.calls[-1]
    assert_reasoning_kept_out_of(answer, reasoning)
    assert_answer_is(answer, REASONING_ANSWER)


@pytest.mark.parametrize("reads_reasoning", [True, False], ids=["reasoning-consumer", "no-consumer"])
async def test_claude_5_tool_loop_through_stream_with_tools(provider, reads_reasoning):
    sent = RequestSpy(provider.client.messages, "stream")
    reasoning = ReasoningCollector() if reads_reasoning else None

    result = await call_api(run_tool_loop(provider, model=CLAUDE_5_MODEL, on_thinking=reasoning, max_tokens=2048))

    assert_tool_loop_answered(result)
    expected = SUMMARIZED_THINKING if reads_reasoning else None
    assert [call.get("thinking") for call in sent.calls] == [expected] * len(sent.calls)
    assert not leaked_reasoning(result.final.content, reasoning)


async def test_claude_5_tool_loop_with_the_thinking_replay_on(provider, monkeypatch):
    setting = REPLAY_ENV.lower()
    if setting not in type(settings).model_fields:
        pytest.skip(
            f"{REPLAY_ENV} is not a setting of the code under test "
            "(the guarded replay is being built on feat/anthropic-thinking-replay)"
        )
    monkeypatch.setattr(settings, setting, True)
    monkeypatch.setenv(REPLAY_ENV, "true")
    sent = RequestSpy(provider.client.messages, "stream")
    reasoning = ReasoningCollector()

    result = await call_api(run_tool_loop(provider, model=CLAUDE_5_MODEL, on_thinking=reasoning, max_tokens=2048))

    assert_tool_loop_answered(result)
    if result.responses[0].thinking:
        # The first round thought before calling the tool: the next request hands it back.
        follow_up = next(call for call in sent.calls if any(m["role"] == "assistant" for m in call["messages"]))
        replayed = next(m for m in follow_up["messages"] if m["role"] == "assistant")
        opening = replayed["content"][0] if isinstance(replayed["content"], list) else {}
        assert opening.get("type") in ("thinking", "redacted_thinking"), (
            f"{REPLAY_ENV} is on but the replayed assistant turn opens with "
            f"{opening.get('type')!r}; if the replay reads a field the agent loop now "
            "carries onto the assistant message, mirror it in "
            "live_llm_support.append_assistant_turn"
        )


def _is_lowest_effort_form(params: dict) -> bool:
    """``thinking: {"type": "adaptive"}`` at the lowest ``output_config.effort``."""
    thinking = params.get("thinking") or {}
    effort = (params.get("output_config") or {}).get("effort")
    return (
        thinking.get("type") == "adaptive"
        and thinking.get("display") != "summarized"
        and effort == anthropic_provider._LOWEST_EFFORT
    )


async def test_mythos_accepts_the_lowest_effort_utility_request(provider, monkeypatch):
    messages = [
        ChatMessage(role="system", content=UTILITY_SYSTEM),
        ChatMessage(role="user", content=UTILITY_PROMPT),
    ]

    async def utility_call():
        return await provider.chat(messages=messages, model=MYTHOS_MODEL, max_tokens=1024, disable_thinking=True)

    native = await capture_request(provider.client.messages, "create", utility_call)
    if not _is_lowest_effort_form(native):
        # The code under test does not send Mythos that form by itself yet: put the
        # provider in the state it reaches once it has learned a model refuses
        # ``thinking.type.disabled``, so ``chat()`` builds the form itself.
        monkeypatch.setattr(anthropic_provider, "_MODELS_REFUSING_DISABLED_THINKING", {MYTHOS_MODEL})
        monkeypatch.setattr(anthropic_provider, "_defaults_to_adaptive_thinking", lambda _model: True)
    sent = RequestSpy(provider.client.messages, "create")

    response = await call_api(utility_call(), no_access_skip=f"this key has no access to {MYTHOS_MODEL}")

    assert sent.calls
    assert all(_is_lowest_effort_form(call) for call in sent.calls), (
        f"not the lowest-effort form: thinking={sent.calls[0].get('thinking')!r}, "
        f"output_config={sent.calls[0].get('output_config')!r}"
    )
    assert all("temperature" not in call for call in sent.calls)
    assert response.content.strip(), f"no text returned (stop_reason={response.stop_reason})"


async def test_sonnet_5_5_accepts_the_between_tools_utility_request(provider):
    """Sonnet 5.5 refuses ``disabled``: a ``disable_thinking`` call sends
    ``between_tools`` alone, at an effort lowered to ``high`` from ``xhigh``,
    which the model refuses alongside it."""
    messages = [
        ChatMessage(role="system", content=UTILITY_SYSTEM),
        ChatMessage(role="user", content=UTILITY_PROMPT),
    ]
    sent = RequestSpy(provider.client.messages, "create")

    response = await call_api(
        provider.chat(
            messages=messages,
            model=SONNET_5_5_MODEL,
            max_tokens=1024,
            disable_thinking=True,
            reasoning_effort="xhigh",
        ),
        no_access_skip=f"this key has no access to {SONNET_5_5_MODEL}",
    )

    assert len(sent.calls) == 1, "the first request was refused and retried"
    assert sent.calls[0].get("thinking") == {"type": "between_tools"}
    assert (sent.calls[0].get("output_config") or {}).get("effort") == "high"
    assert "temperature" not in sent.calls[0]
    assert response.content.strip(), f"no text returned (stop_reason={response.stop_reason})"
