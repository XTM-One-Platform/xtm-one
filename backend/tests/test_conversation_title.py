"""Conversation titles are a structured answer.

Native AI calls (including conversation title generation) run on the
resolved default chat model. Some chat models, given the title prompt, *answer*
the exchange instead of labelling it (e.g. "Hello! Just to clarify, I'm Claude,
an AI assistant made by Anthropic. I don't have any information about emails,
a") and that reply -- truncated at ``max_tokens`` -- became the conversation
title.

The title is asked for as a ``{"title": ...}`` object (native structured output
where the provider has it): an answer that is not that object is no title, and
the caller keeps its short deterministic fallback (the user's first message,
trimmed). No word of the answer is read to decide it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.llm.base import ChatMessage, ChatResponse
from app.services.conversation_memory import (
    _TITLE_SCHEMA,
    _parse_generated_title,
    generate_conversation_title,
)


def _answer(title: str) -> str:
    return json.dumps({"title": title})


class _StubLLM:
    """Minimal LLM stub returning a fixed reply for ``chat`` and recording its kwargs."""

    provider_name = "stub-provider"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def chat(self, *, messages: list[ChatMessage], **kwargs: Any) -> ChatResponse:
        self.calls.append(kwargs)
        return ChatResponse(content=self.reply, model=kwargs.get("model") or "stub")


@pytest.mark.parametrize(
    "raw",
    [
        _answer("Quarterly Sales Review"),
        _answer('"Quarterly Sales Review"'),
        _answer("`Quarterly Sales Review`"),
        _answer("Quarterly Sales Review."),
        _answer("Quarterly   Sales   Review"),
        _answer("Quarterly Sales Review\n(trailing chatter the model added)"),
        '```json\n{"title": "Quarterly Sales Review"}\n```',
    ],
)
def test_the_title_object_is_read_and_cleaned(raw: str) -> None:
    assert _parse_generated_title(raw) == "Quarterly Sales Review"


@pytest.mark.parametrize(
    "raw",
    [
        # The regression class: a chat reply, not the title object.
        "Hello! Just to clarify, I'm Claude, an AI assistant made by Anthropic. I don't have any information",
        "Quarterly Sales Review",
        '{"name": "Quarterly Sales Review"}',
        '["Quarterly Sales Review"]',
        # A title that is not a string, never coerced into one.
        '{"title": {"x": 1}}',
        '{"title": 1234}',
        '{"title": ["Quarterly", "Sales", "Review"]}',
        # The object, holding no title: past the length bound, blank, too short.
        _answer(
            "Detailed quarterly revenue analysis and regional sales "
            "performance breakdown across every business division worldwide"
        ),
        _answer("   "),
        _answer("ab"),
        None,
        "",
        "   ",
    ],
)
def test_an_answer_that_is_not_a_title_object_is_no_title(raw: str | None) -> None:
    assert _parse_generated_title(raw) is None


@pytest.mark.parametrize(
    "title",
    ["History of the Roman Empire", "Sorry Tales Book Club", "Thank You Notes for Customers", "Let Me In Film Review"],
)
def test_a_title_is_never_judged_by_its_words(title: str) -> None:
    assert _parse_generated_title(_answer(title)) == title


def test_generate_title_returns_the_title_of_the_object() -> None:
    llm = _StubLLM(_answer("Quarterly Sales Review"))
    title = asyncio.run(
        generate_conversation_title(
            llm,  # type: ignore[arg-type]
            "Summarise the Q3 sales numbers",
            "Here is the Q3 summary...",
            model="default-chat-model",
        )
    )
    assert title == "Quarterly Sales Review"


def test_generate_title_keeps_the_fallback_for_a_reply_that_is_not_the_object() -> None:
    llm = _StubLLM("Hello! Just to clarify, I'm Claude, an AI assistant made by Anthropic. I don't have any")
    title = asyncio.run(
        generate_conversation_title(
            llm,  # type: ignore[arg-type]
            "Hi Sam, can you check my emails?",
            "Hello! Just to clarify, I'm Claude...",
            model="default-chat-model",
        )
    )
    assert title is None


def test_a_provider_with_structured_output_is_handed_the_title_schema() -> None:
    from app.llm.openai_provider import OpenAIProvider
    from app.services.structured_output import build_openai_response_format

    provider = OpenAIProvider(api_key="dummy")
    sent: list[dict[str, Any]] = []

    async def _chat(**kwargs: Any) -> ChatResponse:
        sent.append(kwargs)
        return ChatResponse(content=_answer("Quarterly Sales Review"), model="gpt-5.5", stop_reason="stop")

    provider.chat = _chat  # type: ignore[method-assign]
    title = asyncio.run(generate_conversation_title(provider, "Q3 sales?", "Here.", model="gpt-5.5"))

    assert title == "Quarterly Sales Review"
    assert sent[0]["structured_output"] == build_openai_response_format(_TITLE_SCHEMA)
