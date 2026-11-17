"""Regression tests: native AI capabilities run on the resolved default model.

Internal / "native" LLM calls (conversation title generation, conversation
compaction, durable-memory fact extraction / reconciliation, style learning,
the activation gate, ...) used to call ``llm.chat()`` *without* a ``model``
argument. That made them fall back to the provider's built-in ``default_model``
instead of the model resolved through the standard chain (agent override ->
agent default -> user preference -> platform default). On a deployment whose
first-configured provider only serves image / embedding models (e.g. an OpenAI
row with ``available_models=["gpt-image-2"]``) this routed chat traffic to a
non-chat / stale model and emitted the recurring warning::

    LLM provider 'openai' exposes no chat-capable model (catalog=['gpt-image-2'])

These tests pin the contract that every native helper *forwards the model it is
given* to ``llm.chat()`` so the resolved default is always honoured.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.llm.base import ChatMessage, ChatResponse


class _RecordingLLM:
    """LLM stub that records the ``model`` of every ``chat`` call."""

    provider_name = "recording-provider"

    def __init__(self, reply: str = "- a durable fact") -> None:
        self.reply = reply
        self.models: list[str | None] = []

    async def chat(self, *, messages: list[ChatMessage], **kwargs: Any) -> ChatResponse:
        self.models.append(kwargs.get("model"))
        return ChatResponse(content=self.reply, model=kwargs.get("model") or "stub")


def test_generate_conversation_title_forwards_model() -> None:
    from app.services.conversation_memory import generate_conversation_title

    llm = _RecordingLLM(reply='{"title": "Quarterly Sales Review"}')
    title = asyncio.run(
        generate_conversation_title(
            llm,  # type: ignore[arg-type]
            "Can you summarise the Q3 sales numbers?",
            "Sure, here is the Q3 summary...",
            model="default-chat-model",
        )
    )

    assert title == "Quarterly Sales Review"
    assert llm.models == ["default-chat-model"]


def test_extract_candidate_facts_forwards_model() -> None:
    from app.services.persistent_memory import _extract_candidate_facts

    llm = _RecordingLLM(reply="- The user prefers the subregion filter")
    facts = asyncio.run(
        _extract_candidate_facts(
            llm,  # type: ignore[arg-type]
            messages_text="user: use the subregion filter, not country",
            is_company_managed=False,
            has_user_scope=True,
            model="default-chat-model",
        )
    )

    assert facts  # at least one fact extracted
    assert llm.models == ["default-chat-model"]


def test_compact_messages_forwards_model() -> None:
    from app.services.conversation_memory import _compact_messages

    llm = _RecordingLLM(
        reply='{"directives": ["use subregion"], "summary": "- discussed dashboards"}'
    )
    messages = [
        ChatMessage(role="user", content="use the subregion filter"),
        ChatMessage(role="assistant", content="understood"),
    ]

    directives, _summary = asyncio.run(
        _compact_messages(llm, messages, model="default-chat-model")  # type: ignore[arg-type]
    )

    assert "use subregion" in directives
    # Both the initial compaction call and any JSON-only retry must carry the
    # resolved model — never None (which would fall back to the provider
    # default).
    assert llm.models  # at least one chat call happened
    assert all(m == "default-chat-model" for m in llm.models)
