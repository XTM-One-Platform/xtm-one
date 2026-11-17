"""The agent loop weighs a message as the provider sends it.

A Gemini tool-calling model turn is kept verbatim in the assistant message's
``_provider_extra["gemini_parts"]`` - the thought parts and the thought
signatures that can ride on any part - and the provider replays it on every
later request of the run, in place of the message content. That replay is
required for function calling and stays verbatim. The loop's gates weighed a
message as ``len(content)`` only, so a transcript whose weight is its thoughts
never reached a compaction threshold and the Gemini provider's own overflow
check was the only safeguard. Pinned here:

* the ONE measure (``message_payload_chars``) is the text plus the provider
  extras, and for a Gemini turn it is what ``_measure_gemini_payload`` counts -
  the signatures included, at their wire size;
* every gate that decides compaction or pruning, the in-loop overflow guard,
  the forced-answer context guard and the emergency prune read it (each of the
  gate tests below fails on the text-only measure);
* a gate that drops a message drops its extras with it, one that truncates a
  message in place re-measures it, and the parts stay verbatim throughout;
* a message without provider extras weighs exactly its text, and any provider
  reports its own extras through the hook.
"""

from __future__ import annotations

import base64
import json

import pytest
from google.genai import types

from app.llm import base as llm_base
from app.llm.base import ChatMessage, ChatResponse, ContextOverflowError
from app.llm.gemini_provider import GeminiProvider
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition
from tests.test_semantic_compaction import _StubSummarizerLLM
from tests.test_tool_call_dedup import (
    _quiet_langfuse,  # noqa: F401 - pytest fixture
    _ScriptedLLM,
)

#: ``GEMINI_PARTS_KEY`` - the builder test below fails if the provider reads another key.
_GEMINI_PARTS = "gemini_parts"
#: 300 bytes: 400 characters of base64 on the wire.
_SIGNATURE = b"\x8f" * 300
_SIGNATURE_WIRE_CHARS = len(base64.b64encode(_SIGNATURE))
_SUMMARY = "SUMMARY: the earlier records were looked up."
# Over the forced-answer backstop's 100-character floor, so it is taken as is.
_ANSWER = "All the records are in: every lookup returned its record, and nothing is missing from the set you asked for."


def _weight(messages: list[ChatMessage]) -> int:
    """The one measure, summed."""
    return sum(llm_base.message_payload_chars(m) for m in messages)


def _text(messages: list[ChatMessage]) -> int:
    """The text-only measure: the message content alone."""
    return sum(len(m.content or "") for m in messages)


def _lookup_call(q: int) -> ToolCall:
    # Gemini mints ``call_{name}_{index}`` per response, so every turn reuses it.
    return ToolCall(id="call_lookup_0", name="lookup", arguments={"q": q})


def _call_chars(call: ToolCall) -> int:
    """What a function-call part carries besides its signature: its name and
    its JSON arguments."""
    return len(call.name) + len(json.dumps(call.arguments, ensure_ascii=False))


def _turn_extras(q: int, thought_chars: int) -> int:
    """What ``_gemini_message(q, thought_chars=...)`` sends beyond its content:
    the thought text, the function call and the signature riding on it."""
    return thought_chars + _call_chars(_lookup_call(q)) + _SIGNATURE_WIRE_CHARS


def _gemini_parts(call: ToolCall, *, thought_chars: int, text: str = "", signed: bool = True) -> list[types.Part]:
    """A tool-calling model turn as ``generate_content`` returns it: a thought
    summary, the answer text if any, and the function call carrying the thought
    signature (Gemini 3 puts it on the first function call part)."""
    parts: list[types.Part] = []
    if thought_chars:
        parts.append(types.Part(text="t" * thought_chars, thought=True))
    if text:
        parts.append(types.Part(text=text))
    parts.append(
        types.Part(
            function_call=types.FunctionCall(name=call.name, args=call.arguments),
            thought_signature=_SIGNATURE if signed else None,
        )
    )
    return parts


def _gemini_message(q: int, *, thought_chars: int, text: str = "") -> ChatMessage:
    """The assistant message the loop appends for a Gemini tool turn."""
    call = _lookup_call(q)
    return ChatMessage(
        role="assistant",
        content=text,
        tool_calls=[call],
        _provider_extra={_GEMINI_PARTS: _gemini_parts(call, thought_chars=thought_chars, text=text)},
    )


def _lookup_result(q: int, chars: int = 100) -> ChatMessage:
    return ChatMessage(role="tool", content=f"record {q}: " + "r" * chars, tool_call_id="call_lookup_0", name="lookup")


def _gemini_turn(q: int, *, thought_chars: int) -> ChatResponse:
    """The response the Gemini provider returns for one tool turn."""
    call = _lookup_call(q)
    return ChatResponse(
        content="",
        model="gemini-3.8-flash",
        stop_reason="tool_use",
        tool_calls=[call],
        _provider_extra={_GEMINI_PARTS: _gemini_parts(call, thought_chars=thought_chars)},
    )


def _gemini_turns(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in messages if m.role == "assistant" and m._provider_extra]


def _assert_verbatim(turns: list[ChatMessage], script: list[ChatResponse], thought_chars: int) -> None:
    """Every Gemini turn still in the transcript replays the parts the provider
    returned: the same list, the thought text and the signature untouched."""
    scripted = [r._provider_extra[_GEMINI_PARTS] for r in script if r._provider_extra]
    for turn in turns:
        parts = turn._provider_extra[_GEMINI_PARTS]
        assert any(parts is original for original in scripted)
        assert parts[0].thought and parts[0].text == "t" * thought_chars
        assert parts[-1].thought_signature == _SIGNATURE


def _lookup_executor() -> CompositeToolExecutor:
    callable_exec = CallableToolExecutor()
    definition = ToolDefinition(
        name="lookup",
        description="look a record up",
        input_schema={"type": "object", "properties": {"q": {"type": "integer"}}},
        source="integration:crm",
    )

    async def _handler(**kwargs):
        return f"record {kwargs.get('q')}: " + "r" * 40

    callable_exec.register("lookup", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools([definition], callable_exec)
    return composite


class _GeminiShapedLLM(_ScriptedLLM):
    """A scripted Gemini tool loop whose every request goes through the Gemini
    provider's own code: its message builder, its payload counter and its
    overflow check at this window. ``refused`` lists the requests (1-based) the
    provider would have refused, ``request_chars`` what each one carried."""

    context_window = 20_000

    def __init__(self, script: list[ChatResponse], *, summary: str = _SUMMARY) -> None:
        super().__init__(script)
        self._gemini = GeminiProvider(api_key="dummy")
        self._gemini.context_window = self.context_window
        self._summary = summary
        self.summaries = 0
        self.request_chars: list[int] = []
        self.refused: list[int] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        if messages and messages[0].content == al._IN_LOOP_COMPACTION_SYSTEM_PROMPT:
            self.summaries += 1
            return ChatResponse(content=self._summary, model="stub")
        system, contents = await self._gemini._build_gemini_contents(messages)
        text_chars, images, native_bytes = GeminiProvider._measure_gemini_payload(system, contents)
        self.request_chars.append(text_chars)
        try:
            self._gemini._check_resolved_overflow(
                text_chars=text_chars,
                image_count=images,
                native_file_bytes=native_bytes,
                tools=tools,
                model=None,
            )
        except ContextOverflowError:
            self.refused.append(len(self.request_chars))
        return await super().chat(messages, model=model, tools=tools, **kwargs)


# ---------------------------------------------------------------------------
# The measure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gemini_turn_weighs_what_the_provider_sends_for_it():
    """Through the provider's own builder and counter: the transcript weighs
    exactly the characters ``_measure_gemini_payload`` counts - the model turn
    is sent as its parts (thought text, answer text, the function call and the
    signature on the wire), never as its ``content``."""
    system = ChatMessage(role="system", content="You look records up.")
    user = ChatMessage(role="user", content="look up record 1")
    turn = _gemini_message(1, thought_chars=4_000, text="Checking record 1.")
    provider = GeminiProvider(api_key="dummy")
    instruction, contents = await provider._build_gemini_contents([system, user, turn])
    text_chars, _images, _files = GeminiProvider._measure_gemini_payload(instruction, contents)

    assert _weight([system, user, turn]) == text_chars
    assert llm_base.provider_extra_chars(turn) == _turn_extras(1, 4_000)


def test_the_signature_is_counted_when_no_thought_text_comes_back():
    """An assignment run reads no reasoning, so Gemini returns no thought text:
    the function call and the signature riding on it are the whole of the
    extras, and Google bills the signature as input when it is sent back."""
    call = _lookup_call(1)
    parts = _gemini_parts(call, thought_chars=0)
    turn = ChatMessage(role="assistant", content="", tool_calls=[call], _provider_extra={_GEMINI_PARTS: parts})
    text_chars, _images, _files = GeminiProvider._measure_gemini_payload(None, [types.Content(role="model", parts=parts)])

    assert text_chars == _call_chars(call) + _SIGNATURE_WIRE_CHARS
    assert llm_base.message_payload_chars(turn) == text_chars
    unsigned = _gemini_parts(call, thought_chars=0, signed=False)
    assert GeminiProvider._measure_gemini_payload(None, [types.Content(role="model", parts=unsigned)])[0] == _call_chars(call)


def test_function_calls_and_responses_weigh_what_they_carry():
    """A model turn replays its function calls verbatim, arguments included, and
    a tool result goes out as a function response: both are counted the way
    ``_measure_oai_payload`` counts tool calls, so a call with a large
    structured argument is not reduced to its signature."""
    call = ToolCall(id="call_update_record_0", name="update_record", arguments={"id": 7, "notes": "n" * 5_000})
    turn = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[call],
        _provider_extra={_GEMINI_PARTS: _gemini_parts(call, thought_chars=0)},
    )
    assert _call_chars(call) > 5_000
    assert llm_base.message_payload_chars(turn) == _call_chars(call) + _SIGNATURE_WIRE_CHARS

    payload = {"result": "r" * 3_000}
    reply = types.Part.from_function_response(name="update_record", response=payload)
    text_chars, _images, _files = GeminiProvider._measure_gemini_payload(None, [types.Content(role="user", parts=[reply])])
    assert text_chars == len("update_record") + len(json.dumps(payload))


def test_a_non_ascii_argument_weighs_its_characters_not_their_escapes():
    """JSON writes a non-ASCII character as a six-character escape by default:
    a Japanese or French argument weighs what it says, not up to six times
    as much, so a CJK run does not compact on escapes it never sends."""
    body = "報告書の一行\n" * 1_000
    call = ToolCall(id="call_generate_file_0", name="generate_file", arguments={"name": "report.md", "content": body})
    turn = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[call],
        _provider_extra={_GEMINI_PARTS: _gemini_parts(call, thought_chars=0)},
    )

    assert len(body) < llm_base.message_payload_chars(turn) < 2 * len(body)


@pytest.mark.asyncio
async def test_the_providers_own_check_never_weighs_a_tool_loop_below_the_loop():
    """Through the provider's own builder a tool result is a function-response
    part: the check that guards the request weighs it, and never weighs the
    request below what the loop's gates weighed it at."""
    user = ChatMessage(role="user", content="look up record 1")
    turn = _gemini_message(1, thought_chars=1_000)
    result = _lookup_result(1, chars=50_000)
    provider = GeminiProvider(api_key="dummy")
    instruction, contents = await provider._build_gemini_contents([user, turn, result])
    text_chars, _images, _files = GeminiProvider._measure_gemini_payload(instruction, contents)

    assert text_chars >= _weight([user, turn, result]) > 50_000


def test_truncating_a_gemini_turn_in_place_is_re_measured_not_counted_as_progress():
    """The emergency prune truncates long messages in place. A Gemini turn is
    replayed as its parts whatever its ``content`` says, so a shorter content
    frees nothing: the measure stays what the provider sends, and the parts
    are left exactly as they were."""
    turn = _gemini_message(1, thought_chars=2_000, text="A" * 3_000)
    parts = turn._provider_extra[_GEMINI_PARTS]
    before = llm_base.message_payload_chars(turn)
    assert before == 3_000 + _turn_extras(1, 2_000)

    turn.content = al._emergency_truncate_text(turn.content, 500)

    assert len(turn.content) < 3_000
    assert llm_base.message_payload_chars(turn) == before
    assert turn._provider_extra[_GEMINI_PARTS] is parts
    assert [p.text for p in parts[:2]] == ["t" * 2_000, "A" * 3_000]


def test_a_message_without_provider_extras_weighs_exactly_its_text():
    """No behaviour change for any provider that keeps no replay data: every
    role, an empty extras dict, an extras key no provider counts, and an empty
    Gemini part list (the provider then sends the content) weigh their text."""
    call = _lookup_call(1)
    plain = [
        ChatMessage(role="system", content="s" * 120),
        ChatMessage(role="user", content="u" * 45),
        ChatMessage(role="assistant", content="a" * 300, tool_calls=[call]),
        ChatMessage(role="tool", content="r" * 700, tool_call_id="call_lookup_0", name="lookup"),
        ChatMessage(role="assistant", content=""),
        ChatMessage(role="assistant", content="b" * 80, _provider_extra={}),
        ChatMessage(role="assistant", content="c" * 80, _provider_extra={"not_a_provider_key": ["x" * 999]}),
        ChatMessage(role="assistant", content="d" * 80, tool_calls=[call], _provider_extra={_GEMINI_PARTS: []}),
    ]
    for message in plain:
        assert llm_base.provider_extra_chars(message) == 0
        assert llm_base.message_payload_chars(message) == len(message.content or "")

    text = _text(plain)
    assert al._estimate_prompt_tokens(plain) == text // al._CHARS_PER_TOKEN
    assert sum(al._context_breakdown_chars(plain, None).values()) == text
    assert al._context_budget_state(plain, 128_000)["transcript_chars"] == text


def test_any_provider_reports_its_extras_through_the_hook(monkeypatch):
    """The hook is not Gemini's: a provider that keeps replay data under its own
    key (the Anthropic thinking-block replay, next) registers a counter and
    every gate weighs it. A counter never makes a message lighter than its
    text."""
    monkeypatch.setattr(llm_base, "_PROVIDER_EXTRA_COUNTERS", dict(llm_base._PROVIDER_EXTRA_COUNTERS))

    def _thinking_blocks(message: ChatMessage, blocks: object) -> int:
        return sum(len(b["thinking"]) + len(b["signature"]) for b in blocks)

    llm_base.register_provider_extra_counter("test_thinking_blocks", _thinking_blocks)
    llm_base.register_provider_extra_counter("test_negative", lambda message, value: -50)
    turn = ChatMessage(
        role="assistant",
        content="ok",
        _provider_extra={"test_thinking_blocks": [{"thinking": "t" * 500, "signature": "s" * 100}]},
    )
    assert llm_base.message_payload_chars(turn) == 2 + 600
    assert al._context_breakdown_chars([turn], None)["conversation"] == 602
    assert al._estimate_prompt_tokens([turn]) == 602 // al._CHARS_PER_TOKEN
    lighter = ChatMessage(role="assistant", content="abc", _provider_extra={"test_negative": True})
    assert llm_base.message_payload_chars(lighter) == 3


def test_a_counter_that_raises_never_fails_the_turn(monkeypatch, caplog):
    """The measure runs inside every gate of every iteration: a broken counter
    counts zero and is logged once, instead of failing the turn it measures."""
    monkeypatch.setattr(llm_base, "_PROVIDER_EXTRA_COUNTERS", dict(llm_base._PROVIDER_EXTRA_COUNTERS))
    monkeypatch.setattr(llm_base, "_FAILED_PROVIDER_EXTRA_KEYS", set())

    def _broken(message: ChatMessage, value: object) -> int:
        raise TypeError("unexpected replay data")

    llm_base.register_provider_extra_counter("test_broken", _broken)
    turn = ChatMessage(role="assistant", content="hello", _provider_extra={"test_broken": object()})
    with caplog.at_level("WARNING", logger="app.llm.base"):
        assert llm_base.message_payload_chars(turn) == 5
        assert al._transcript_chars([turn, turn]) == 10
    assert sum("test_broken" in r.getMessage() for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# The gates, one by one
# ---------------------------------------------------------------------------


def test_the_session_prune_starts_on_the_payload_not_the_text():
    """Six turns whose text is under the budget and whose thoughts are not: the
    prune now stubs the older tool results, and what it frees is what the
    measure loses."""
    messages = [ChatMessage(role="user", content="look up records 1 to 6")]
    for q in range(1, 7):
        messages += [_gemini_message(q, thought_chars=3_000), _lookup_result(q, chars=1_000)]
    budget = 10_000
    before = _text(messages) + sum(_turn_extras(q, 3_000) for q in range(1, 7))
    assert _text(messages) < budget < before
    results = [m for m in messages if m.role == "tool"]
    bodies = [len(m.content) for m in results]

    al._prune_session_context(messages, max_session_chars=budget)

    pruned = [m for m in results if m.content.startswith("[Pruned tool result")]
    assert pruned == results[: len(results) - al._RECENT_TOOL_RESULTS_KEEP]
    freed = sum(bodies[i] - len(results[i].content) for i in range(len(pruned)))
    assert before - _weight(messages) == freed


def test_the_emergency_prune_evicts_by_what_the_turns_carry():
    """Eight turns, 5k characters of thought each, a few hundred characters of
    text in all: the emergency prune now evicts the oldest groups until the
    payload fits, their extras going with them, and the turns it keeps replay
    their parts verbatim with every tool result still after its call."""
    messages = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="task")]
    for q in range(1, 9):
        messages += [_gemini_message(q, thought_chars=5_000), _lookup_result(q)]
    original = list(messages)
    parts_before = {id(m): m._provider_extra[_GEMINI_PARTS] for m in _gemini_turns(messages)}
    budget = 30_000
    before = _text(messages) + sum(_turn_extras(q, 5_000) for q in range(1, 9))
    assert _text(messages) < budget < before

    al._emergency_prune_session_context(messages, max_session_chars=budget)

    kept = {id(m) for m in messages}
    dropped = [m for m in original if id(m) not in kept]
    assert dropped, "the oldest groups were evicted"
    assert _weight(messages) <= budget
    assert before - _weight(messages) == _weight(dropped)
    for turn in _gemini_turns(messages):
        assert turn._provider_extra[_GEMINI_PARTS] is parts_before[id(turn)]
        index = messages.index(turn)
        assert messages[index + 1].role == "tool"


@pytest.mark.asyncio
async def test_semantic_compaction_weighs_the_middle_with_its_thoughts():
    """The middle it would summarise is two turns of thought and two short
    results: "too little to gain" on the text, 11k characters on the wire. It
    compacts now, the summary replacing the turns and their extras."""
    llm = _StubSummarizerLLM()
    messages = [ChatMessage(role="user", content="look up records 1 to 6")]
    for q in range(1, 7):
        messages += [_gemini_message(q, thought_chars=5_000), _lookup_result(q)]
    middle = messages[1:5]
    middle_weight = _text(middle) + _turn_extras(1, 5_000) + _turn_extras(2, 5_000)
    assert _text(middle) < al._SEMANTIC_COMPACTION_MIN_DROP_CHARS <= middle_weight
    before = _text(messages) + sum(_turn_extras(q, 5_000) for q in range(1, 7))

    compacted = await al._semantic_compact_session_context(llm, messages, model=None, reason="test")

    assert compacted and llm.calls == 1
    summary = next(m for m in messages if al._SEMANTIC_COMPACTION_MARKER in (m.content or ""))
    assert all(m not in messages for m in middle)
    assert before - _weight(messages) == middle_weight - len(summary.content)
    assert len(_gemini_turns(messages)) == 4


@pytest.mark.asyncio
async def test_empty_response_recovery_sees_a_large_context_in_its_thoughts():
    """An empty turn on a large context goes to semantic compaction, a small one
    to the cheap prune only. Eight turns of 8k-character thoughts are large."""
    llm = _StubSummarizerLLM()
    messages = [ChatMessage(role="user", content="look up records 1 to 8")]
    for q in range(1, 9):
        messages += [_gemini_message(q, thought_chars=8_000), _lookup_result(q)]
    weight = _text(messages) + sum(_turn_extras(q, 8_000) for q in range(1, 9))
    assert _text(messages) < al._EMPTY_RESPONSE_RECOVERY_TRIGGER_CHARS < weight

    total = await al._compact_for_empty_recovery(llm, messages, model=None, max_session_chars=10**9)

    assert llm.calls == 1, "semantic compaction was attempted"
    assert total == _weight(messages)


def test_the_context_readout_and_the_budget_reservation_count_the_thoughts():
    """The readout says how full the window is and the per-run budget reserves
    a call's worst case: both carry the thoughts the request carries."""
    user = ChatMessage(role="user", content="look up record 1")
    turn = _gemini_message(1, thought_chars=3_000)
    expected = len(user.content) + _turn_extras(1, 3_000)

    assert al._context_breakdown_chars([user, turn], None)["conversation"] == expected
    assert al._estimate_prompt_tokens([user, turn]) == expected // al._CHARS_PER_TOKEN
    assert al._context_budget_state([user, turn], 128_000)["transcript_chars"] == expected


# ---------------------------------------------------------------------------
# End to end: the loop never sends what its provider refuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
@pytest.mark.parametrize("summary", [_SUMMARY, ""], ids=["semantic-compaction", "emergency-prune"])
async def test_a_gemini_tool_loop_never_sends_a_request_its_provider_refuses(summary):
    """Seven tool turns whose weight is their 10k-character thoughts, content
    empty. On the text measure no gate ever fired, and from the seventh request
    on the Gemini provider's own overflow check refused what the loop sent.
    Weighed with the extras, the compaction gate summarises the oldest turns -
    or, when the summariser fails, the overflow guard's emergency prune evicts
    them - and every request fits."""
    script = [_gemini_turn(q, thought_chars=10_000) for q in range(1, 8)]
    script.append(ChatResponse(content=_ANSWER, model="gemini-3.8-flash"))
    llm = _GeminiShapedLLM(script, summary=summary)
    composite = _lookup_executor()
    messages = [ChatMessage(role="user", content="look up records 1 to 7")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=12,
        progressive_tools=False,
    )

    assert response.content == _ANSWER
    assert len(llm.request_chars) == len(script)
    assert llm.refused == [], llm.request_chars
    turns = _gemini_turns(messages)
    assert len(turns) < 7, "the oldest turns were compacted or evicted"
    _assert_verbatim(turns, script, 10_000)
    if summary:
        assert llm.summaries >= 1
        assert any(al._SEMANTIC_COMPACTION_MARKER in (m.content or "") for m in messages)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_the_forced_answer_synthesis_is_measured_with_the_thoughts(monkeypatch):
    """The iteration cap lands right after the sixth turn, so the synthesis is
    the first request past the window and the forced-answer context guard
    is what must bring it under. On the text measure it let the
    request through to the provider's refusal; weighed with the extras, its
    emergency prune evicts the oldest turn and the synthesis fits. The in-loop
    gate is held shut so the synthesis is the one request that crosses."""
    monkeypatch.setattr(al, "_COMPACTION_TRIGGER_UTILIZATION", 10.0)
    script = [_gemini_turn(q, thought_chars=9_600) for q in range(1, 7)]
    script.append(ChatResponse(content=_ANSWER, model="gemini-3.8-flash"))
    llm = _GeminiShapedLLM(script)
    composite = _lookup_executor()
    messages = [ChatMessage(role="user", content="look up records 1 to 6")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=6,
        max_session_chars=10**9,
        progressive_tools=False,
    )

    assert response.content == _ANSWER
    assert len(llm.request_chars) == len(script)
    assert llm.refused == [], llm.request_chars
    turns = _gemini_turns(messages)
    assert len(turns) == 5, "the oldest turn was evicted before the synthesis"
    _assert_verbatim(turns, script, 9_600)
