"""Every provider sends a turn's tool results in the model's call order.

The agent loop answers some calls of a batch in place before the rest runs - a
validation failure, a loop-breaker refusal, a call cut by the tool-call budget -
so the transcript can hold a turn's results out of call order. Gemini pairs a
``function_response`` with its call by position (the part carries the tool name,
never the call id), and so does whatever an OpenAI-format or Bedrock request
reaches when the id is dropped on the way (a proxy to Gemini, a local model's
chat template). Pinned here:

* ``tool_results_in_call_order`` puts each run of tool results back in call
  order and moves nothing else;
* every provider builder - Anthropic, Bedrock, Gemini, and both OpenAI-format
  builders - sends them in call order;
* a batch the tool-call budget cut, run through the real loop, is a next request
  every provider's builder turns into a payload its API accepts: every call
  answered once, right after it, in call order, by its own result.
"""

from __future__ import annotations

import pytest
from google.genai import types

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import (
    ChatMessage,
    ChatResponse,
    build_openai_messages,
    build_openai_messages_async,
    tool_results_in_call_order,
)
from app.llm.bedrock_provider import BedrockProvider
from app.llm.gemini_provider import GEMINI_PARTS_KEY, GeminiProvider
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition
from app.tools.run_budget import RunBudget
from tests.test_agent_loop_run_budget import (
    _PADDING,
    _quiet_langfuse,  # noqa: F401 - pytest fixture
    _ScriptedLLM,
)


def _calls() -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name="lookup", arguments={"q": str(i)}) for i in (1, 2, 3)]


def _transcript() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", content="", tool_calls=_calls()),
        # Answered in place first, as the loop does for a cut or refused call.
        ChatMessage(role="tool", content="not executed 2", tool_call_id="c2", name="lookup"),
        ChatMessage(role="tool", content="not executed 3", tool_call_id="c3", name="lookup"),
        ChatMessage(role="tool", content="ran 1", tool_call_id="c1", name="lookup"),
        ChatMessage(role="user", content="next"),
    ]


def test_a_tool_run_is_put_back_in_call_order():
    ordered = tool_results_in_call_order(_transcript())

    assert [m.tool_call_id for m in ordered if m.role == "tool"] == ["c1", "c2", "c3"]
    # Nothing outside the run moves.
    assert [m.role for m in ordered] == ["user", "assistant", "tool", "tool", "tool", "user"]
    assert ordered[-1].content == "next"


def test_a_result_the_turn_does_not_know_stays_last():
    msgs = _transcript()
    msgs.insert(3, ChatMessage(role="tool", content="stray", tool_call_id="zz", name="lookup"))

    ordered = tool_results_in_call_order(msgs)

    assert [m.tool_call_id for m in ordered if m.role == "tool"] == ["c1", "c2", "c3", "zz"]


# ── What each provider sends ─────────────────────────────────────────────


async def _anthropic_sent(messages: list[ChatMessage]) -> list[str]:
    _system, conversation = await AnthropicProvider(api_key="dummy")._build_anthropic_messages(messages)
    return [
        block["content"]
        for entry in conversation
        if entry["role"] == "user" and isinstance(entry["content"], list)
        for block in entry["content"]
        if block.get("type") == "tool_result"
    ]


async def _bedrock_sent(messages: list[ChatMessage]) -> list[str]:
    _system, converse = await BedrockProvider(api_key="a||b||us-east-1")._build_converse_messages(messages)
    return [
        block["toolResult"]["content"][0]["text"]
        for entry in converse
        for block in entry["content"]
        if "toolResult" in block
    ]


async def _gemini_sent(messages: list[ChatMessage]) -> list[str]:
    _system, contents = await GeminiProvider(api_key="dummy")._build_gemini_contents(messages)
    return [
        part.function_response.response["result"]
        for content in contents
        for part in content.parts or []
        if getattr(part, "function_response", None)
    ]


async def _openai_sent(messages: list[ChatMessage]) -> list[str]:
    payload = await build_openai_messages_async(messages, "openai")
    return [entry["content"] for entry in payload if entry["role"] == "tool"]


async def _openai_chat_sent(messages: list[ChatMessage]) -> list[str]:
    return [entry["content"] for entry in build_openai_messages(messages) if entry["role"] == "tool"]


_SENT_RESULTS = {
    "anthropic": _anthropic_sent,
    "bedrock": _bedrock_sent,
    "gemini": _gemini_sent,
    "openai": _openai_sent,
    "openai-chat": _openai_chat_sent,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(_SENT_RESULTS))
async def test_every_provider_sends_the_results_in_call_order(provider):
    assert await _SENT_RESULTS[provider](_transcript()) == ["ran 1", "not executed 2", "not executed 3"]


# ── A cut batch, through the real loop ───────────────────────────────────


def _answered_right_after(entries: list, i: int, answers) -> list[str]:
    """The call ids the entries following ``entries[i]`` answer, per *answers*
    (an entry -> ids function, empty for an entry that answers nothing)."""
    answered: list[str] = []
    for follower in entries[i + 1 :]:
        ids = answers(follower)
        if not ids:
            break
        answered.extend(ids)
    return answered


async def _pairing_problems(messages: list[ChatMessage]) -> list[str]:
    """Why each provider's API would refuse the request built from *messages*.

    Anthropic and Bedrock need every ``tool_use`` / ``toolUse`` answered in the
    next user turn, OpenAI a ``tool`` message per ``tool_call_id`` right after
    the assistant turn, Gemini as many function responses as function calls -
    which it pairs by POSITION, so each response must also be its own call's.
    Order is checked on every provider: that is the parity this pins.
    """
    problems: list[str] = []

    payload = await build_openai_messages_async(messages, "openai")
    for i, entry in enumerate(payload):
        ids = [tc["id"] for tc in entry.get("tool_calls") or []]
        if entry["role"] == "assistant" and ids:
            answered = _answered_right_after(
                payload, i, lambda e: [e["tool_call_id"]] if e["role"] == "tool" else []
            )
            if answered != ids:
                problems.append(f"openai: calls {ids} answered by {answered}")

    _system, conversation = await AnthropicProvider(api_key="dummy")._build_anthropic_messages(messages)
    for i, entry in enumerate(conversation):
        blocks = entry["content"] if isinstance(entry["content"], list) else []
        ids = [block["id"] for block in blocks if block.get("type") == "tool_use"]
        if entry["role"] == "assistant" and ids:
            nxt = conversation[i + 1] if i + 1 < len(conversation) else {"role": None, "content": []}
            answered = [
                block["tool_use_id"]
                for block in (nxt["content"] if isinstance(nxt["content"], list) else [])
                if block.get("type") == "tool_result"
            ]
            if nxt["role"] != "user" or answered != ids:
                problems.append(f"anthropic: tool_use {ids} answered by {answered}")

    _system_parts, converse = await BedrockProvider(api_key="a||b||us-east-1")._build_converse_messages(messages)
    for i, entry in enumerate(converse):
        ids = [block["toolUse"]["toolUseId"] for block in entry["content"] if "toolUse" in block]
        if entry["role"] == "assistant" and ids:
            nxt = converse[i + 1] if i + 1 < len(converse) else {"role": None, "content": []}
            answered = [block["toolResult"]["toolUseId"] for block in nxt["content"] if "toolResult" in block]
            if nxt["role"] != "user" or answered != ids:
                problems.append(f"bedrock: toolUse {ids} answered by {answered}")

    _instruction, contents = await GeminiProvider(api_key="dummy")._build_gemini_contents(messages)
    result_of = {m.tool_call_id: m.content for m in messages if m.role == "tool"}
    turns = [
        m for m in messages if m.role == "assistant" and (getattr(m, "_provider_extra", None) or {}).get(GEMINI_PARTS_KEY)
    ]
    calling = [
        i
        for i, content in enumerate(contents)
        if content.role == "model" and any(p.function_call for p in content.parts or [])
    ]
    for turn, i in zip(turns, calling, strict=True):
        expected = [result_of.get(tc.id) for tc in turn.tool_calls]
        nxt = contents[i + 1] if i + 1 < len(contents) else None
        sent = [
            part.function_response.response["result"]
            for part in (nxt.parts or [] if nxt is not None else [])
            if part.function_response
        ]
        if nxt is None or nxt.role != "user" or sent != expected:
            problems.append(f"gemini: calls answered {expected} expected, {sent} sent")
    return problems


class _EveryProviderLLM(_ScriptedLLM):
    """Builds every provider's payload from each request the loop makes."""

    def __init__(self, script: list[ChatResponse]) -> None:
        super().__init__(script)
        self.requests: list[list[ChatMessage]] = []
        self.problems: list[str] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.requests.append(list(messages))
        self.problems.extend(await _pairing_problems(messages))
        return await super().chat(messages, model=model, tools=tools, **kwargs)


def _lookup_executor() -> tuple[CompositeToolExecutor, list[int]]:
    executed: list[int] = []
    callable_exec = CallableToolExecutor()
    definition = ToolDefinition(
        name="lookup",
        description="look a record up",
        input_schema={"type": "object", "properties": {"q": {"type": "integer"}}},
        source="custom:test",
    )

    async def _handler(**kwargs):
        executed.append(kwargs["q"])
        return f"record {kwargs['q']}"

    callable_exec.register("lookup", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools([definition], callable_exec)
    return composite, executed


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_a_batch_the_budget_cut_is_a_next_request_every_provider_accepts():
    # Gemini-shaped, ids included, so the Gemini builder replays the function
    # calls it pairs the responses with.
    calls = [ToolCall(id=f"call_lookup_{q}", name="lookup", arguments={"q": q}) for q in range(5)]
    parts = [types.Part(function_call=types.FunctionCall(name=c.name, args=c.arguments)) for c in calls]
    llm = _EveryProviderLLM(
        [
            ChatResponse(
                content="",
                model="gemini-3.8-flash",
                stop_reason="tool_use",
                tool_calls=calls,
                usage={},
                _provider_extra={GEMINI_PARTS_KEY: parts},
            ),
            ChatResponse(content="Two of the five lookups ran." + _PADDING, model="gemini-3.8-flash", usage={}),
        ]
    )
    composite, executed = _lookup_executor()

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="look them all up")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_tool_calls=2, strict_tool_calls=True),
    )

    assert executed == [0, 1]
    assert response.content == "Two of the five lookups ran." + _PADDING
    assert len(llm.requests) == 2
    assert llm.problems == []
    # The transcript holds the three cut calls' answers FIRST - they are
    # appended before the kept calls run - and every provider still sends
    # the five in call order, each with its own result.
    after_cut = llm.requests[-1]
    assert [m.tool_call_id for m in after_cut if m.role == "tool"] == [
        "call_lookup_2",
        "call_lookup_3",
        "call_lookup_4",
        "call_lookup_0",
        "call_lookup_1",
    ]
    notice = al._format_tool_budget_refusal(RunBudget(max_tool_calls=2))
    for provider, sent in _SENT_RESULTS.items():
        results = await sent(after_cut)
        assert results == ["record 0", "record 1", notice, notice, notice], provider
