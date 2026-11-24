"""The prune / re-call / breaker cycle of the 25 Sep 2026 run.

The harness cleared results the model had not used yet, told it to re-call,
served the re-call from the cache under a "use the previous result" nudge
(the previous result being the stub), then flagged the same re-calls as
repeated. The rules that close the cycle, each pinned here:

* a tool result produced in the CURRENT turn is never stubbed by the
  byte-budget pass or the compaction gate's prune until an assistant message
  with substantive text follows it (``_unwritten_turn_results``);
* a dedup cache hit replays the FULL payload and never points the model at a
  stub;
* the loop breaker forgives a re-call the curation invited through ONE rule,
  the held-answer rule (pinned in ``test_loop_breaker.py``), and
  treats a page series on one target as one read going somewhere - exempt
  from the ``similar`` count, bounded by its own page budget.

Plus the transcript-derived preload of tool groups across turns.
"""

from __future__ import annotations

import pytest

from app.llm.base import ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.agent_loop import (
    _DEDUP_CACHE_MARKER,
    _loop_breaker_page,
    _LoopBreaker,
    _unwritten_turn_results,
)
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)
from app.tools.tool_gating import LOAD_TOOLS_TOOL_NAME, build_tool_gate
from tests.test_tool_call_dedup import (
    _quiet_langfuse,  # noqa: F401 - pytest fixture
    _ScriptedLLM,
    _tool_call_response,
    _tool_messages,
)

_BIG = 20_000


def _breaker(**overrides) -> _LoopBreaker:
    kwargs = {"identical_warn": 3, "identical_refuse": 5, "similar_warn": 6, "similar_refuse": 10}
    kwargs.update(overrides)
    return _LoopBreaker(**kwargs)


def _reader_executor(payload_chars: int = _BIG) -> tuple[CompositeToolExecutor, list[dict]]:
    """A ``get_page`` read tool whose result is *payload_chars* long."""
    executed: list[dict] = []
    callable_exec = CallableToolExecutor()
    definition = ToolDefinition(
        name="get_page",
        description="read a page",
        input_schema={"type": "object", "properties": {"q": {"type": "integer"}}},
        source="integration:notion",
    )

    async def _handler(**kwargs):
        executed.append(dict(kwargs))
        return f"page {kwargs.get('q')} " + ("x" * payload_chars)

    callable_exec.register("get_page", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools([definition], callable_exec)
    return composite, executed


def _call(call_id: str, q: int) -> ToolCall:
    return ToolCall(id=call_id, name="get_page", arguments={"q": q})


async def _run(llm, composite, **kwargs):
    messages = [ChatMessage(role="user", content="analyse the plan")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        progressive_tools=False,
        **kwargs,
    )
    return response, messages


def _stubbed(messages) -> list[str]:
    return [m.tool_call_id for m in messages if m.role == "tool" and al._is_curation_stub(m.content or "")]


def _answer(breaker: _LoopBreaker, transcript: list[ChatMessage], tool: str, args: dict, body: str) -> ChatMessage:
    """Append + record the tool message answering the call just screened."""
    answer = ChatMessage(role="tool", content=body, tool_call_id=f"c{len(transcript)}", name=tool)
    transcript.append(answer)
    breaker.record_answer(tool, args, answer)
    return answer


# ---------------------------------------------------------------------------
# Unit: what "written about" means
# ---------------------------------------------------------------------------


def test_unwritten_turn_results_are_those_no_substantive_assistant_text_follows():
    history_result = ChatMessage(role="tool", content="old", tool_call_id="h1", name="get_page")
    r1 = ChatMessage(role="tool", content="a" * 1000, tool_call_id="c1", name="get_page")
    r2 = ChatMessage(role="tool", content="b" * 1000, tool_call_id="c2", name="get_page")
    r3 = ChatMessage(role="tool", content="c" * 1000, tool_call_id="c3", name="get_page")
    messages = [
        ChatMessage(role="user", content="q"),
        history_result,
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1", 1), _call("c2", 2)]),
        r1,
        r2,
        # A short preamble with the next batch says nothing about the data.
        ChatMessage(role="assistant", content="Let me check HiBob now.", tool_calls=[_call("c3", 3)]),
        r3,
    ]
    is_history = lambda m: m is history_result  # noqa: E731
    protected = _unwritten_turn_results(messages, is_history=is_history)
    assert protected == {id(r1), id(r2), id(r3)}, "nothing substantive was written yet"
    assert id(history_result) not in protected, "the previous turns' results stay under the budget"

    # Substantive text after r1 / r2 releases them; r3 came after it.
    messages[5] = ChatMessage(
        role="assistant",
        content="The strategy page lists objectives 1.1 to 1.4: " + "detail " * 130,
        tool_calls=[_call("c3", 3)],
    )
    protected = _unwritten_turn_results(messages, is_history=is_history)
    assert protected == {id(r3)}


def test_substantive_means_past_the_loops_own_preamble_line():
    """200 characters released a batch at every
    narrated step, while the loop itself holds back up to 900 characters of
    pre-tool text as a PREAMBLE (stage plans run 350-900). One line, two uses."""
    assert al._SUBSTANTIVE_TEXT_MIN_CHARS == al._PREAMBLE_HOLDBACK_CHARS
    r1 = ChatMessage(role="tool", content="a" * 1000, tool_call_id="c1", name="get_page")
    plan = "Let me now read the HiBob headcount and the Metabase pipeline before I compare them to the plan. " * 6
    messages = [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1", 1)]),
        r1,
        ChatMessage(role="assistant", content=plan[:600], tool_calls=[_call("c2", 2)]),
    ]
    assert _unwritten_turn_results(messages, is_history=lambda m: False) == {id(r1)}, "a stage plan is a preamble"


# ---------------------------------------------------------------------------
# Unit: the breaker's paging exemption and its bound
# ---------------------------------------------------------------------------


def test_loop_breaker_page_reads_the_position_argument():
    assert _loop_breaker_page({"file_id": "f", "offset": 50000}) == 50000.0
    assert _loop_breaker_page({"file_id": "f", "offset": "90000"}) == 90000.0
    assert _loop_breaker_page({"file_id": "f", "cursor": "abc"}) == "abc"
    assert _loop_breaker_page({"file_id": "f", "page": 3, "offset": 0}) == 0.0, "first present wins"
    assert _loop_breaker_page({"file_id": "f", "max_chars": 1000}) is None
    assert _loop_breaker_page({"file_id": "f", "offset": None}) is None
    assert _loop_breaker_page({"file_id": "f", "offset": float("inf")}) is None
    assert _loop_breaker_page({"file_id": "f", "cursor": "x" * 600}) is None, "data, not a position token"
    assert _loop_breaker_page(None) is None


def test_a_timestamp_or_a_date_is_never_a_page_position():
    """``start`` is a timestamp as often as an index, and a time-range scan
    asks a new question each call: only a NUMBER makes ``start`` a position,
    and a date never is one, whatever the argument is called."""
    assert _loop_breaker_page({"url": "u", "start": 40}) == 40.0
    assert _loop_breaker_page({"url": "u", "start": "2026-09-01T00:00:00Z"}) is None
    assert _loop_breaker_page({"url": "u", "start": "yesterday"}) is None
    assert _loop_breaker_page({"url": "u", "after": "2026-09-01"}) is None
    assert _loop_breaker_page({"url": "u", "after": "2026-09-01 12:30"}) is None
    assert _loop_breaker_page({"url": "u", "after": "Y3Vyc29yOjQy"}) == "Y3Vyc29yOjQy"
    # A time-range scan on one target is therefore counted like any repeat.
    breaker = _breaker(identical_warn=0, identical_refuse=0, similar_warn=3, similar_refuse=0)
    actions = [
        breaker.screen("query_metrics", {"url": "https://m.test/q", "start": f"2026-09-0{day}T00:00:00Z"}).action
        for day in range(1, 4)
    ]
    assert actions == ["allow", "allow", "warn"]
    assert breaker.events[-1].kind == "similar"


def test_a_forward_page_series_on_one_target_is_not_a_repeat():
    """``read_file_content(offset=0)``, ``(offset=50000)``, ... twelve pages of
    one file: a series going somewhere, never the ``repeated call on one
    target`` warning the 25 Sep 2026 run got after six."""
    breaker = _breaker()
    actions = [
        breaker.screen("read_file_content", {"file_id": "abc", "offset": 50_000 * i}).action
        for i in range(12)
    ]
    assert actions == ["allow"] * 12
    assert breaker.events == []


def test_re_reading_a_held_position_is_still_counted():
    """Going back to a page the model still holds, or varying ``max_chars`` at
    the same offset (the cache-defeat pattern), is a repeat on the
    target; a position nobody read yet is a page, wherever it lies."""
    breaker = _breaker(identical_warn=0, identical_refuse=0, similar_warn=3, similar_refuse=0)
    key = ("read_file_content", "file_id", "abc")
    for i in range(3):
        assert breaker.screen("read_file_content", {"file_id": "abc", "offset": 50_000 * i}).action == "allow"
    assert breaker._page_counts[key] == 3
    actions = [
        breaker.screen("read_file_content", {"file_id": "abc", "offset": 0, "max_chars": 1000 + i}).action
        for i in range(3)
    ]
    assert actions == ["allow", "allow", "warn"], "three re-reads of page one warn at the similar bound"
    assert breaker.events[-1].kind == "similar"
    # A read naming no position reads the start, which is held.
    breaker.screen("read_file_content", {"file_id": "abc", "max_chars": 5})
    assert len(breaker._similar[key]) == 4
    # An unread position - behind the furthest one or past it - is a page.
    breaker.screen("read_file_content", {"file_id": "abc", "offset": 25_000})
    breaker.screen("read_file_content", {"file_id": "abc", "offset": 200_000})
    assert len(breaker._similar[key]) == 4
    assert breaker._page_counts[key] == 5


def test_string_cursors_page_too_and_a_write_resets_the_pages():
    breaker = _breaker(identical_warn=0, identical_refuse=0, similar_warn=2, similar_refuse=0)
    assert breaker.screen("list_rows", {"url": "https://x.test/rows", "cursor": "p1"}).action == "allow"
    assert breaker.screen("list_rows", {"url": "https://x.test/rows", "cursor": "p2"}).action == "allow"
    assert breaker.screen("list_rows", {"url": "https://x.test/rows", "cursor": "p1"}).action == "allow"
    assert breaker.screen("list_rows", {"url": "https://x.test/rows", "cursor": "p2"}).action == "warn"
    breaker.after_write()
    assert breaker._pages == {} and breaker._page_counts == {}
    assert breaker.screen("list_rows", {"url": "https://x.test/rows", "cursor": "p1"}).action == "allow"


def test_a_write_starts_a_checks_page_series_over_but_not_the_writers_own():
    """The page ledger follows the write rule: every tool that is not
    a write BY ITS NAME starts over - an unknown-verb check included - while
    the tools that wrote in the step keep their series, like their other
    counts, so a paged write repeated past its budget is still caught."""
    assert not al._is_named_write("capture_rows") and not al._is_named_write("reconcile_rows"), "premise: unknown verbs"
    breaker = _breaker()
    rows = {"url": "https://x.test/rows"}
    for cursor in ("c1", "c2"):
        breaker.screen("capture_rows", {**rows, "cursor": cursor})
        breaker.screen("reconcile_rows", {**rows, "cursor": cursor})
    breaker.after_write({"reconcile_rows"})
    remaining = {key[0] for key in breaker._page_counts}
    assert remaining == {"reconcile_rows"}, "the unknown-verb check started over, the writer did not"
    assert breaker._page_counts[("reconcile_rows", "url", "https://x.test/rows")] == 2


def test_a_page_series_is_bounded_by_the_page_budget():
    """The exemption has a bound: pages served on one target are counted on
    the page ledger and warned / refused at the factor times the similar
    bounds - an endless cursor cannot run to ``max_iterations`` unwarned. The
    page texts never claim the result would not differ: a new page does."""
    breaker = _breaker(identical_warn=0, identical_refuse=0, similar_warn=2, similar_refuse=4)
    factor = al._LOOP_BREAKER_PAGE_BUDGET_FACTOR
    decisions = [
        breaker.screen("list_rows", {"url": "https://x.test/rows", "cursor": f"c{i}"})
        for i in range(4 * factor + 1)
    ]
    actions = [d.action for d in decisions]
    assert actions.index("warn") == 2 * factor - 1, "warned at the factor times the similar warn bound"
    assert actions.index("refuse") == 4 * factor - 1, "refused at the factor times the similar refuse bound"
    assert actions.count("warn") == 1, "one-shot"
    assert all(e.kind == "page" for e in breaker.events)
    warn = next(e for e in breaker.events if e.action == "warn")
    refusal = al._format_loop_breaker_refusal("list_rows", breaker.events[-1])
    nudge = al._format_loop_breaker_nudge("list_rows", warn)
    assert "page budget" in refusal and "would not differ" not in refusal
    assert "pages of the same url" in nudge and "would not differ" not in nudge
    assert "pages of the same url" in al._format_loop_breaker_user_warning("list_rows", warn)
    # Disabling the similar bounds disables the page budget with them.
    off = _breaker(identical_warn=0, identical_refuse=0, similar_warn=0, similar_refuse=0)
    assert not off.enabled


def test_a_cleared_page_is_free_again_but_the_budget_never_forgives():
    """The ONE forgiveness mechanism frees the position of a page
    whose answer the curation cleared - the re-read the stub invited is a page
    again, not a similar repeat - while the page COUNT keeps rising, so a
    cursor whose older pages keep being cleared still reaches its bound."""
    transcript: list[ChatMessage] = []
    breaker = _breaker(identical_warn=0, identical_refuse=0, transcript=transcript)
    key = ("read_file_content", "file_id", "doc")
    answers = []
    for i in range(4):
        args = {"file_id": "doc", "offset": i * 10_000}
        assert breaker.screen("read_file_content", args).action == "allow"
        answers.append(_answer(breaker, transcript, "read_file_content", args, "page " * 300))
    for answer in answers:
        answer.content = al._REFETCHABLE_STUB_TEMPLATE.format(
            target="", tool=answer.name, chars=f"{len(answer.content):,}"
        )
    breaker.screen("read_file_content", {"file_id": "doc", "offset": 0})
    assert key not in breaker._similar, "the invited re-read is not a repeat on the target"
    assert breaker._page_counts[key] == 5, "but it is one more page served"
    # A HELD page read again is a repeat.
    held = {"file_id": "doc", "offset": 0}
    _answer(breaker, transcript, "read_file_content", held, "page " * 300)
    breaker.screen("read_file_content", {"file_id": "doc", "offset": 0, "max_chars": 10})
    assert len(breaker._similar[key]) == 1


def test_a_refused_page_asked_again_is_refused_again():
    """The refused ask stayed in the page ledger
    WITH its position, so the re-ask looked like a re-read of a held page,
    went to the ``similar`` ledger (count 1) and ran - the most common model
    behaviour, retrying, defeated the page budget. A refusal read nothing."""
    breaker = _breaker(identical_warn=0, identical_refuse=0, similar_warn=2, similar_refuse=4)
    budget = 4 * al._LOOP_BREAKER_PAGE_BUDGET_FACTOR
    rows = {"url": "https://x.test/rows"}
    for i in range(budget - 1):
        assert breaker.screen("list_rows", {**rows, "cursor": f"c{i}"}).action != "refuse"
    last = {**rows, "cursor": f"c{budget - 1}"}
    assert breaker.screen("list_rows", last).action == "refuse"
    for _ in range(3):
        again = breaker.screen("list_rows", last)
        assert again.action == "refuse" and again.kind == "page", "the retry is the next page, refused in turn"
    assert ("list_rows", "url", "https://x.test/rows") not in breaker._similar


def test_distinct_queries_of_one_file_are_positions_not_repeats():
    """``query_file_json`` is where this PR sends the model for embedded data:
    ten different JSONPaths of one plan are ten questions, not a loop, and the
    messages call them queries. The same query again is still a repeat."""
    breaker = _breaker()
    decisions = [
        breaker.screen("query_file_json", {"file_id": "plan", "path": f"$.plan.regions[{i}].name"}) for i in range(12)
    ]
    assert [d.action for d in decisions] == ["allow"] * 12 and breaker.events == []
    assert _loop_breaker_page({"file_id": "p", "path": "$.a", "block": 2}, "query_file_json") == "path=$.a|block=2"
    long_path = "$." + "k" * 700
    position = _loop_breaker_page({"file_id": "p", "path": long_path}, "query_file_json")
    assert position.startswith("sha:") and len(position) < 64, "bounded, still one position per read"
    budget = _breaker(identical_warn=0, identical_refuse=0, similar_warn=2, similar_refuse=4)
    for i in range(2 * al._LOOP_BREAKER_PAGE_BUDGET_FACTOR):
        budget.screen("query_file_json", {"file_id": "plan", "path": f"$.q{i}"})
    warn = budget.events[0]
    assert warn.kind == "page"
    assert "6 queries of the same file_id" in al._format_loop_breaker_nudge("query_file_json", warn)
    repeat = _breaker(identical_warn=2, identical_refuse=0)
    repeat.screen("query_file_json", {"file_id": "plan", "path": "$.a"})
    assert repeat.screen("query_file_json", {"file_id": "plan", "path": "$.a"}).action == "warn"


def test_paging_arguments_are_read_in_any_spelling_and_an_empty_one_is_skipped():
    """An MCP server spells its cursor ``pageToken`` / ``startAt`` / ``nextCursor``;
    an empty value names nothing (the next argument may), and a negative offset
    reads the start like offset 0 does."""
    assert _loop_breaker_page({"url": "u", "startAt": 50}) == 50.0
    assert _loop_breaker_page({"url": "u", "pageToken": "tok-2"}) == "tok-2"
    assert _loop_breaker_page({"url": "u", "nextCursor": "n3"}) == "n3"
    assert _loop_breaker_page({"url": "u", "Start-Index": "20"}) == 20.0
    assert _loop_breaker_page({"url": "u", "offset": "", "cursor": "c9"}) == "c9"
    assert _loop_breaker_page({"url": "u", "offset": -1}) == _loop_breaker_page({"url": "u", "offset": 0}) == 0.0


def test_the_turn_protection_is_bounded_newest_first():
    """A tool-call-only turn used to keep EVERY result out of the byte budget
    up to the compaction trigger. The protection now holds the newest results
    up to its budget; the older ones fall back under the ordinary budget."""
    results = [ChatMessage(role="tool", content=c * 1_000, tool_call_id=f"c{i}", name="get_page") for i, c in enumerate("abcd")]
    messages = [ChatMessage(role="user", content="q")]
    for result in results:
        messages.append(ChatMessage(role="assistant", content="", tool_calls=[_call(result.tool_call_id, 1)]))
        messages.append(result)
    never = lambda m: False  # noqa: E731
    assert _unwritten_turn_results(messages, is_history=never) == {id(r) for r in results}
    assert _unwritten_turn_results(messages, is_history=never, budget_chars=2_500) == {id(results[2]), id(results[3])}
    assert _unwritten_turn_results(messages, is_history=never, budget_chars=0) == set()


def test_a_replay_is_the_full_payload_under_one_header_and_never_reads_as_a_stub():
    """The replay carries the cached content whole (already cut by the
    per-result cap when it was first appended, so it is bounded) under ONE
    header - which no rule that recognises a stub, a files note or an
    attachment claim may mistake for one: a replay read as a stub would be
    forgiven by the breaker, re-stubbed as "already cleared", or stripped."""
    replay = al._format_cached_result("page 1 " + "x" * 500)
    assert replay.startswith(_DEDUP_CACHE_MARKER + " ")
    assert replay.endswith("\n\npage 1 " + "x" * 500)
    assert replay.count(_DEDUP_CACHE_MARKER) == 1
    assert not al._is_curation_stub(replay)
    assert not al._is_refetchable_stub(replay)
    assert not al._FILES_REMOVED_NOTE_RE.search(replay)
    assert not al._ATTACHMENT_CLAIM_RE.search(replay)
    assert "Use the previous result" not in replay


# ---------------------------------------------------------------------------
# Integration: the loop keeps this turn's material until the model wrote about it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_current_turn_results_survive_the_byte_budget_until_written_about():
    """Three 20 KB reads over two tool-call-only batches: 60 KB over the 48 KB
    live floor, and not one of them is stubbed before the answer."""
    composite, executed = _reader_executor()
    llm = _ScriptedLLM(
        [
            _tool_call_response(_call("c1", 1), _call("c2", 2)),
            _tool_call_response(_call("c3", 3)),
            ChatResponse(content="Here is what the plan says.", model="stub"),
        ]
    )
    _response, messages = await _run(llm, composite)
    assert [e["q"] for e in executed] == [1, 2, 3]
    assert _stubbed(messages) == []
    for cid in ("c1", "c2", "c3"):
        assert _tool_messages(messages, cid)[0].content.startswith(f"page {cid[1]} xxx")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_the_compaction_gate_prune_skips_unwritten_turn_results_too(monkeypatch):
    """The gate forced open on every iteration: a batch the
    model has SEEN but only answered with more tool calls is not the
    gate's to stub - semantic compaction, then the emergency prune, are the
    backstops for a transcript that would not fit."""
    monkeypatch.setattr(al, "_COMPACTION_TRIGGER_UTILIZATION", 0.0)
    monkeypatch.setattr(al, "_COMPACTION_TARGET_UTILIZATION", 0.0)
    composite, _executed = _reader_executor(payload_chars=300)
    llm = _ScriptedLLM(
        [
            _tool_call_response(*[_call(f"a{i}", i) for i in range(6)]),
            _tool_call_response(*[_call(f"b{i}", 10 + i) for i in range(6)]),
            ChatResponse(content="Read everything.", model="stub"),
        ]
    )
    _response, messages = await _run(llm, composite)
    assert _stubbed(messages) == []
    assert not any((m.content or "").startswith("[Pruned tool result") for m in messages if m.role == "tool")


class _WindowLLM(_ScriptedLLM):
    """A scripted model with a small window that records every request's size."""

    context_window = 20_000

    def __init__(self, script: list[ChatResponse]) -> None:
        super().__init__(script)
        self.request_chars: list[int] = []
        #: Every request's tool messages, as ``{tool_call_id: content}``.
        self.request_tools: list[dict[str, str]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.request_chars.append(sum(len(m.content or "") for m in messages))
        self.request_tools.append({m.tool_call_id: m.content or "" for m in messages if m.role == "tool"})
        return await super().chat(messages, model=model, tools=tools, **kwargs)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_no_request_exceeds_the_window_the_forced_answer_included():
    """The turn protection keeps unwritten results through the byte budget and
    the gate's prune, so the window is what bounds a request. Two tool-call-only
    batches of four capped reads hit ``max_iterations``: the last batch was
    never sent, and the protected results alone are past the window. The
    in-loop requests stay under the compaction trigger; the forced-answer
    synthesis is brought under the window by the same emergency prune the
    in-loop guard uses, instead of being sent overflowing."""
    answer = "Answer from what was read: the eight pages cover the plan's objectives, owners and quarterly targets in full."
    llm = _WindowLLM(
        [
            _tool_call_response(*[_call(f"a{i}", i) for i in range(4)]),
            _tool_call_response(*[_call(f"b{i}", 10 + i) for i in range(4)]),
            ChatResponse(content=answer, model="stub"),
        ]
    )
    composite, executed = _reader_executor()
    window_chars = llm.context_window * al._CHARS_PER_TOKEN
    per_result_cap = min(int(llm.context_window * al._TOOL_RESULT_FRACTION * al._CHARS_PER_TOKEN), al._MAX_TOOL_RESULT_CHARS_CAP)
    assert 8 * per_result_cap > window_chars * 0.95, "the premise: the protected results alone overflow"
    messages = [ChatMessage(role="user", content="analyse the plan")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=2,
        progressive_tools=False,
    )
    assert len(executed) == 8
    assert response.content == answer
    assert len(llm.request_chars) == 3
    assert max(llm.request_chars) <= window_chars * 0.95, llm.request_chars
    # The synthesis exists to show the last
    # batch, and stubbing the older, already-seen one is enough to fit - the
    # protection gives way to the window BEFORE the emergency prune, which
    # truncated every long message (b0..b3 came out at ~2,000 chars each).
    synthesis = llm.request_tools[-1]
    for cid in ("b0", "b1", "b2", "b3"):
        assert synthesis[cid].startswith(f"page 1{cid[1]} xxx") and len(synthesis[cid]) > 5_000, (cid, len(synthesis[cid]))
    assert all(al._is_curation_stub(synthesis[f"a{i}"]) for i in range(4)), "the older batch gave way"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_results_written_about_fall_back_under_the_ordinary_budget():
    """Once the model has written about the first batch, the oldest result
    over the live budget is cleared as before - the turn protection is
    about unused material, not a longer window for everything."""
    composite, _executed = _reader_executor()
    written = "From the strategy page: objectives 1.1 to 1.4 target NA growth, " + "detail " * 130
    llm = _ScriptedLLM(
        [
            _tool_call_response(_call("c1", 1), _call("c2", 2)),
            ChatResponse(content=written, model="stub", stop_reason="tool_calls", tool_calls=[_call("c3", 3)]),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    _response, messages = await _run(llm, composite)
    # At the top of iteration 3 (before the request that carries c3): c1 and
    # c2 are written about and seen, c3 is unseen; c2 + c3 = 40 KB fit the
    # 48 KB floor, c1 does not.
    assert _stubbed(messages) == ["c1"]
    assert "cleared to keep the working context focused" in _tool_messages(messages, "c1")[0].content
    assert _tool_messages(messages, "c2")[0].content.startswith("page 2 xxx")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_a_re_call_after_the_platform_cleared_the_result_is_replayed_in_full_and_forgiven():
    """The cycle of the 25 Sep 2026 run, closed: the stub invites a re-call,
    the re-call is served from the cache with the FULL payload (never "use
    the previous result"), and the held-answer rule forgives the ask
    whose answer the curation cleared - the third ask of the same call would
    otherwise have earned the ``repeated identical call`` warning."""
    from app.core.config import settings

    assert settings.agent_loop_identical_call_warn_after == 3, "the default the run hit"
    composite, executed = _reader_executor()
    written = "From the strategy page: objectives 1.1 to 1.4 target NA growth, " + "detail " * 130
    llm = _ScriptedLLM(
        [
            _tool_call_response(_call("c1", 1), _call("c2", 2)),
            ChatResponse(content=written, model="stub", stop_reason="tool_calls", tool_calls=[_call("c3", 3)]),
            # c1 is stubbed at the top of this iteration; the model re-calls it.
            _tool_call_response(_call("c4", 1)),
            # ... and again, with the replay above intact: THIS one counts.
            _tool_call_response(_call("c5", 1)),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    _response, messages = await _run(llm, composite)
    assert [e["q"] for e in executed] == [1, 2, 3], "the re-calls were served from the cache"
    assert al._is_refetchable_stub(_tool_messages(messages, "c1")[0].content), "the premise: the curation cleared c1"
    replay = _tool_messages(messages, "c5")[0]
    assert replay.content.startswith(_DEDUP_CACHE_MARKER)
    assert "Use the previous result" not in replay.content
    assert "page 1 " + "x" * _BIG in replay.content, "the full payload, not a stub"
    # c4 was the same full replay; once c5 landed, the duplicate collapse
    # folded the older copy into a pointer at the newer one (never a pointer
    # at a stub).
    assert _tool_messages(messages, "c4")[0].content.startswith("[Duplicate get_page result collapsed")
    # Three asks of get_page(q=1) this turn, the first one's answer cleared by
    # the platform: the identical-call warning (bound 3) did not fire.
    system_texts = [m.content for m in messages if m.role == "system"]
    assert not any("repeated identical call" in text for text in system_texts)
    assert not any("[Loop breaker] Refused" in (m.content or "") for m in messages if m.role == "tool")


# ---------------------------------------------------------------------------
# Persistent tool groups across turns
# ---------------------------------------------------------------------------


def _tools(provider: str, n: int) -> list[ToolDefinition]:
    return [
        ToolDefinition(name=f"{provider}_tool_{i}", description=f"{provider} {i}", source=f"integration:{provider}")
        for i in range(n)
    ]


def test_preload_from_transcript_reveals_the_groups_earlier_turns_used():
    tools = [
        ToolDefinition(name="search_knowledge", description="kb", source="builtin:knowledge"),
        *_tools("notion", 6),
        *_tools("hibob", 6),
        *_tools("metabase", 6),
        *_tools("jira", 6),
    ]
    gate = build_tool_gate(tools, enabled=True, min_tools=1)
    assert gate is not None
    transcript = [
        ChatMessage(role="user", content="first question"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="l1", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["hibob", "integration:metabase"]})],
        ),
        ChatMessage(role="tool", content="Loaded.", tool_call_id="l1", name=LOAD_TOOLS_TOOL_NAME),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="notion_tool_2", arguments={})]),
        ChatMessage(role="tool", content="page", tool_call_id="c1", name="notion_tool_2"),
        ChatMessage(role="assistant", content="Here is the answer from the previous turn."),
        ChatMessage(role="user", content="follow-up question"),
    ]
    revealed = gate.preload_from_transcript(transcript)
    assert revealed == ["integration:hibob", "integration:metabase", "integration:notion"]
    visible = {t.name for t in gate.visible_tools()}
    assert {"hibob_tool_0", "metabase_tool_5", "notion_tool_2"} <= visible
    assert not any(name.startswith("jira_") for name in visible), "a group no turn used stays gated"
    # No system-prompt line of its own (invariant 16): the tools are on the
    # wire, and the catalogue already says a visible tool needs no loading.
    assert "Already loaded" not in gate.catalog_prompt
    assert "Tools already visible to you require no loading" in gate.catalog_prompt
    # An unknown name or a tool the agent no longer carries is skipped, and
    # what an earlier pass named stays named.
    assert gate.preload_from_transcript(
        [ChatMessage(role="tool", content="x", tool_call_id="z", name="gone_tool")]
    ) == []
    assert gate.preloaded_group_ids == revealed


def test_preload_keeps_the_most_recently_used_groups_under_the_provider_cap():
    """LRU under the cap: with room for one group, the group the conversation
    used LAST is the one on the wire."""
    tools = [*_tools("notion", 4), *_tools("hibob", 4)]
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=5)
    assert gate is not None
    transcript = [
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="a", name="notion_tool_0", arguments={})]),
        ChatMessage(role="tool", content="x", tool_call_id="a", name="notion_tool_0"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="b", name="hibob_tool_0", arguments={})]),
        ChatMessage(role="tool", content="y", tool_call_id="b", name="hibob_tool_0"),
    ]
    revealed = gate.preload_from_transcript(transcript)
    assert revealed == ["integration:hibob"]
    assert gate.revealed_group_ids == {"integration:hibob"}
    assert gate.preloaded_group_ids == ["integration:hibob"]


def test_preload_reveals_only_the_most_recently_used_groups():
    """A long conversation can touch many integrations: revealing all of them
    would put every schema on every request, the flat-catalogue cost the gate
    exists to avoid. Only the ``_PRELOAD_MAX_GROUPS`` used last come back; an
    older one is a ``load_tools`` away. A ``groups`` argument sent as one
    string (not a list) counts too."""
    from app.tools.tool_gating import _PRELOAD_MAX_GROUPS

    providers = [f"p{i}" for i in range(_PRELOAD_MAX_GROUPS + 4)]
    tools = [t for p in providers for t in _tools(p, 2)]
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=200)
    assert gate is not None
    transcript: list[ChatMessage] = []
    for i, provider in enumerate(providers[:-1]):
        transcript.append(
            ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=f"c{i}", name=f"{provider}_tool_0", arguments={})])
        )
        transcript.append(ChatMessage(role="tool", content="x", tool_call_id=f"c{i}", name=f"{provider}_tool_0"))
    transcript.append(
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="l", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": providers[-1]})],
        )
    )
    revealed = gate.preload_from_transcript(transcript)
    assert revealed == [f"integration:{p}" for p in providers[-_PRELOAD_MAX_GROUPS:]]
    visible = {t.name for t in gate.visible_tools()}
    assert "p0_tool_0" not in visible and f"{providers[-1]}_tool_1" in visible


def test_preload_names_only_groups_that_are_fully_on_the_wire():
    """A group the cap can only reveal PARTIALLY keeps tools hidden, so the
    catalogue must not call it "already loaded ... callable right now"; a
    second pass that finds it at capacity changes nothing either."""
    tools = _tools("notion", 8)
    gate = build_tool_gate(tools, enabled=True, min_tools=1, max_visible=5)
    assert gate is not None
    transcript = [
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="a", name="notion_tool_0", arguments={})]),
        ChatMessage(role="tool", content="x", tool_call_id="a", name="notion_tool_0"),
    ]
    assert gate.preload_from_transcript(transcript) == []
    assert gate.partially_revealed_group_ids == {"integration:notion"}, "revealed as far as the cap allows"
    assert gate.preloaded_group_ids == []
    # At capacity on the second pass: still nothing to name.
    assert gate.preload_from_transcript(transcript) == []
    assert gate.preloaded_group_ids == []


@pytest.mark.parametrize("junk", [5, True, 3.5, {"a": 1}, None, [7, None, {"x": 1}]])
def test_a_malformed_historical_load_tools_call_costs_the_preload_never_the_turn(junk):
    """The history replays a ``load_tools`` call's
    arguments as the model sent them, and ``groups=5`` raised TypeError before
    the first request - on every later turn of that conversation."""
    tools = [*_tools("notion", 4), *_tools("hibob", 4)]
    gate = build_tool_gate(tools, enabled=True, min_tools=1)
    assert gate is not None
    transcript = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="l", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": junk, "tools": junk})],
        ),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="a", name="notion_tool_0", arguments={})]),
    ]
    assert gate.preload_from_transcript(transcript) == ["integration:notion"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_a_preload_that_raises_never_fails_the_turn(monkeypatch):
    from app.tools.tool_gating import ToolGate
    from tests.test_progressive_tool_disclosure import _executor_with_groups

    def _boom(self, messages):
        raise RuntimeError("history the preload cannot read")

    monkeypatch.setattr(ToolGate, "preload_from_transcript", _boom)
    composite, _executed = _executor_with_groups(("notion", 10))
    llm = _ScriptedLLM([ChatResponse(content="Still answered.", model="stub")])
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="hello")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=3,
        progressive_tools=True,
    )
    assert response.content == "Still answered."


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_a_follow_up_turn_starts_with_the_groups_the_previous_turn_loaded():
    """End to end: the second turn's first request already carries the Notion
    schemas, so the model calls the tool without a ``load_tools`` round-trip."""
    from tests.test_progressive_tool_disclosure import _executor_with_groups

    composite, executed = _executor_with_groups(("notion", 10), ("hibob", 10))
    history = [
        ChatMessage(role="user", content="read the strategy page"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="l1", name=LOAD_TOOLS_TOOL_NAME, arguments={"groups": ["notion"]})],
        ),
        ChatMessage(role="tool", content="Loaded Notion.", tool_call_id="l1", name=LOAD_TOOLS_TOOL_NAME),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="notion_tool_1", arguments={})]),
        ChatMessage(role="tool", content="ran notion_tool_1", tool_call_id="c1", name="notion_tool_1"),
        ChatMessage(role="assistant", content="The page says X."),
        ChatMessage(role="user", content="and the second section?"),
    ]
    llm = _ScriptedLLM(
        [
            _tool_call_response(ToolCall(id="c2", name="notion_tool_3", arguments={})),
            ChatResponse(content="Section two says Y.", model="stub"),
        ]
    )
    response = await al.run_agent_loop(
        llm=llm,
        messages=history,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        progressive_tools=True,
    )
    assert response.content == "Section two says Y."
    assert executed == ["notion_tool_3"]
    first_offer = llm.offered_tools[0]
    assert "notion_tool_3" in first_offer, "the Notion group was on the wire before any load_tools call"
    assert not any(name.startswith("hibob_") for name in first_offer)


# ---------------------------------------------------------------------------
# The 25 Sep 2026 incident, end to end
# ---------------------------------------------------------------------------


class _RecordingLLM(_ScriptedLLM):
    """A 200k-token model (the incident's class) that records every request."""

    context_window = 200_000

    def __init__(self, script: list[ChatResponse]) -> None:
        super().__init__(script)
        self.requests: list[list[ChatMessage]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.requests.append([ChatMessage(role=m.role, content=m.content, tool_call_id=m.tool_call_id) for m in messages])
        return await super().chat(messages, model=model, tools=tools, **kwargs)


def _incident_plan() -> bytes:
    """A ~560k self-contained plan: styles, SVG geometry, a JavaScript data literal, prose."""
    import json

    accounts = [{"name": f"Account {i}", "segment": ["Federal", "State", "City"][i % 3], "acv": 60_000 + i} for i in range(1_500)]
    html = (
        "<!doctype html><html><head><title>Americas Coverage Model</title>"
        "<style>" + ".cell{color:#0FBCFF} " * 6_000 + "</style>"
        f"<script>const ACCOUNTS = {json.dumps(accounts)};</script></head><body>"
        "<h1>North America plan</h1><p>Three pods, 40% growth, FY27 new ARR ceiling $9.43M.</p>"
        "<svg>" + "".join(f"<path d='M {i} {i} L {i + 1} {i + 2} Z'/>" for i in range(12_000)) + "</svg>"
        + "".join(f"<p>Narrative paragraph {i} of the plan.</p>" for i in range(600))
        + "</body></html>"
    )
    return html.encode("utf-8")


def _incident_executor(monkeypatch):
    import uuid
    from types import SimpleNamespace

    from app.llm.base import FileRef
    from app.services.builtin_tools import build_generate_file_tool
    from app.tools.base import FileContext

    async def _fake_persist(**kwargs):
        return SimpleNamespace(id=uuid.uuid4(), filename=kwargs["filename"], content_type=kwargs["content_type"])

    monkeypatch.setattr("app.services.builtin_tools._persist_generated_file", _fake_persist)
    defs, file_tools = build_generate_file_tool(db=None, user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    ctx = FileContext()
    ref = FileRef.from_bytes(_incident_plan(), "Filigran_Americas_Coverage_Model TD.html", "text/html")
    assert len(ref.inline_data) > 500_000, "the premise: the incident's size class"
    ctx.add(ref)
    file_tools.file_context = ctx
    composite = CompositeToolExecutor()
    composite.add_tools(defs, file_tools)
    return composite, ctx, ref


_BREAKER_MARKERS = (
    "[Loop breaker]",
    "[SYSTEM - repeated identical call]",
    "[SYSTEM - repeated call on one target]",
    "[SYSTEM - long page series]",
)


def _loop_breaker_traces(messages) -> list[str]:
    return [m.content or "" for m in messages if any(marker in (m.content or "") for marker in _BREAKER_MARKERS)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_the_incident_question_is_answered_in_three_requests(monkeypatch):
    """"Tim has sent this plan, what do you think?" with the 559k HTML plan:
    97 iterations on 25 Sep 2026. The rendered view carries the page's data
    and text in ONE read, one query reads a value, and the answer follows -
    three requests, nothing stubbed, no loop-breaker message."""
    composite, ctx, ref = _incident_executor(monkeypatch)
    answer = "The plan is directional: three pods and a 40% growth target, with a $9.43M FY27 ceiling. " * 4
    llm = _RecordingLLM(
        [
            _tool_call_response(ToolCall(id="r1", name="read_file_content", arguments={"file_id": ref.file_id, "mode": "extract_structured"})),
            _tool_call_response(ToolCall(id="q1", name="query_file_json", arguments={"file_id": ref.file_id, "path": "$[0].acv", "block": 1})),
            ChatResponse(content=answer, model="stub"),
        ]
    )
    messages = [ChatMessage(role="user", content="Tim has sent this plan, what do you think?")]
    response = await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite,
        max_iterations=10, progressive_tools=False, file_context=ctx,
    )
    assert response.content == answer
    assert len(llm.requests) == 3
    rendered = _tool_messages(messages, "r1")[0].content
    assert "## Embedded data" in rendered and "// 1,500 records" in rendered
    assert "North America plan" in rendered and "<path" not in rendered and ".cell{" not in rendered
    assert "Content (complete," in rendered, "one window holds the whole rendering"
    assert "60000" in _tool_messages(messages, "q1")[0].content
    assert _stubbed(messages) == [] and _loop_breaker_traces(messages) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_quiet_langfuse")
async def test_paging_the_incident_file_stops_at_the_window_cap_without_a_loop(monkeypatch):
    """The incident's own shape - the model pages the SOURCE 50k at a time:
    four windows are served (the protection keeps them all on a 200k model),
    the fifth call is the explicit cap error naming the rendered view and the
    sandbox mount, the model answers, and the loop breaker never speaks."""
    from app.services.builtin_tools import build_code_interpreter_tool
    from app.services.code_interpreter import mount_path_hint

    composite, ctx, ref = _incident_executor(monkeypatch)
    # The incident's General Assistant offered the code interpreter: the only
    # surface on which the cap names its mount.
    composite.add_tools(*build_code_interpreter_tool())
    answer = "From the four parts I read: the plan sets three pods and a 40% growth target for FY27. " * 4
    script = [
        _tool_call_response(ToolCall(id=f"w{i}", name="read_file_content", arguments={"file_id": ref.file_id, "offset": 50_000 * i, "max_chars": 50_000}))
        for i in range(5)
    ]
    llm = _RecordingLLM([*script, ChatResponse(content=answer, model="stub")])
    messages = [ChatMessage(role="user", content="Tim has sent this plan, what do you think?")]
    response = await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite,
        max_iterations=10, progressive_tools=False, file_context=ctx,
    )
    assert response.content == answer
    assert len(llm.requests) == 6
    refused = _tool_messages(messages, "w4")[0].content
    assert "window budget for this file exhausted this turn, 4 of 4 served ('Filigran_Americas_Coverage_Model TD.html'" in refused
    assert 'mode="extract_structured"' in refused and mount_path_hint(ref.filename, ref.file_id) in refused
    assert ctx.read_window_count(ref.file_id, "source") == 4
    assert _stubbed(messages) == [], "every window was still in front of the model when it answered"
    assert _loop_breaker_traces(messages) == []
