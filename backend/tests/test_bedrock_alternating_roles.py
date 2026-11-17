"""Converse requests alternate between user and assistant.

Bedrock refuses a conversation with two consecutive messages of one role, and
a transcript holds such runs: a message right after the tool results it
follows (steering, the resumed work of an interrupted turn), a mid-turn
segment stored before the answer that continued it.
"""

from __future__ import annotations

from app.llm.base import ChatMessage
from app.llm.bedrock_provider import BedrockProvider
from app.tools.base import ToolCall


async def _converse(messages: list[ChatMessage]) -> list[dict]:
    _system, converse = await BedrockProvider(api_key="a||b||us-east-1")._build_converse_messages(
        messages
    )
    return converse


def _roles(converse: list[dict]) -> list[str]:
    return [entry["role"] for entry in converse]


async def test_a_message_after_tool_results_joins_their_user_turn():
    converse = await _converse(
        [
            ChatMessage(role="system", content="persona"),
            ChatMessage(role="user", content="read the files"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="a", name="read_file", arguments={}),
                    ToolCall(id="b", name="read_file", arguments={}),
                ],
            ),
            ChatMessage(role="tool", content="A", tool_call_id="a", name="read_file"),
            ChatMessage(role="tool", content="B", tool_call_id="b", name="read_file"),
            ChatMessage(role="user", content="you crashed, go on"),
        ]
    )
    assert _roles(converse) == ["user", "assistant", "user"]
    last = converse[-1]["content"]
    assert [block["toolResult"]["toolUseId"] for block in last[:2]] == ["a", "b"]
    assert last[2] == {"text": "you crashed, go on"}


async def test_consecutive_answers_are_one_assistant_turn():
    converse = await _converse(
        [
            ChatMessage(role="user", content="question"),
            ChatMessage(role="assistant", content="segment"),
            ChatMessage(role="assistant", content="final answer"),
            ChatMessage(role="user", content="next"),
        ]
    )
    assert _roles(converse) == ["user", "assistant", "user"]
    assert converse[1]["content"] == [{"text": "segment"}, {"text": "final answer"}]


async def test_a_blank_text_block_is_dropped_from_a_merged_turn():
    converse = await _converse(
        [
            ChatMessage(role="user", content="question"),
            ChatMessage(role="assistant", content=""),
            ChatMessage(role="assistant", content="answer"),
            ChatMessage(role="user", content="next"),
        ]
    )
    assert converse[1]["content"] == [{"text": "answer"}]


async def test_an_alternating_conversation_is_sent_as_built():
    messages = [
        ChatMessage(role="user", content="question"),
        ChatMessage(role="assistant", content="answer"),
        ChatMessage(role="user", content="next"),
    ]
    converse = await _converse(messages)
    assert converse == [
        {"role": "user", "content": [{"text": "question"}]},
        {"role": "assistant", "content": [{"text": "answer"}]},
        {"role": "user", "content": [{"text": "next"}]},
    ]
