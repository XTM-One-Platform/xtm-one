"""Live smoke tests of the OpenRouter provider - opt-in, real cost (see ``README.md``).

OpenRouter streams a reasoning model's thinking in the ``reasoning`` field of
each delta (the streaming path reads it): it must reach ``on_thinking`` and never ``content``,
on the plain streaming path and on the tool-calling one alike.
"""

from __future__ import annotations

import pytest

from app.llm.base import ChatMessage
from tests.live.live_llm_support import (
    LOOKUP_TOOL,
    REASONING_ANSWER,
    REASONING_PROMPT,
    ReasoningCollector,
    assert_answer_is,
    assert_reasoning_kept_out_of,
    build_live_provider,
    call_api,
    close_provider,
    leaked_reasoning,
    live_marks,
    model_for,
    stream_turn,
)

pytestmark = live_marks("openrouter")

# A model that always reasons and returns its reasoning by default.
OPENROUTER_MODEL = model_for("LIVE_LLM_OPENROUTER_MODEL", "deepseek/deepseek-r1")


@pytest.fixture
async def provider():
    llm = build_live_provider("openrouter")
    yield llm
    await close_provider(llm)


async def test_reasoning_streams_to_the_consumer_never_into_content(provider):
    reasoning = ReasoningCollector()

    answer = await call_api(stream_turn(provider, model=OPENROUTER_MODEL, max_tokens=2048, on_thinking=reasoning))

    assert_reasoning_kept_out_of(answer, reasoning)
    assert_answer_is(answer, REASONING_ANSWER)
    assert "<think>" not in answer


async def test_reasoning_stays_out_of_content_with_tools_offered(provider):
    reasoning = ReasoningCollector()

    async def on_token(_text: str) -> None:
        return None

    response = await call_api(
        provider.chat_stream_with_tools(
            messages=[ChatMessage(role="user", content=f"{REASONING_PROMPT} Do not use any tool.")],
            tools=[LOOKUP_TOOL],
            on_token=on_token,
            model=OPENROUTER_MODEL,
            max_tokens=2048,
            on_thinking=reasoning,
        )
    )

    assert response.content.strip() or response.tool_calls, "neither an answer nor a tool call"
    if response.tool_calls:
        assert reasoning.text.strip(), "no reasoning text reached on_thinking"
        assert not leaked_reasoning(response.content, reasoning)
    else:
        assert_reasoning_kept_out_of(response.content, reasoning)
        assert_answer_is(response.content, REASONING_ANSWER)
    assert "<think>" not in response.content
